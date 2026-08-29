#!/usr/bin/env python3
"""Desktop launcher for local Codex, Claude Code, and Antigravity sessions."""

from __future__ import annotations

import json
import collections
import concurrent.futures
import fcntl
import os
import pty
import re
import select
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
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
CODEX_MODELS_CACHE = HOME / ".codex" / "models_cache.json"
CODEX_CONFIG = HOME / ".codex" / "config.toml"
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
# Tracks Claude processes Session Hub itself launched, so a same-directory
# /clear inside that same process (new session id, same PID) can be detected
# and linked to the session it continues - see pid_capture_command and
# resolve_clear_continuations.
PID_DIR = DATA_DIR / "pids"
# One JSON file per session_id (Claude transcript UUID or Codex thread id),
# written by the --hook-notify / --hook-notify-codex handlers and read back
# by refresh_running_tab - see install_status_hooks/install_status_hooks_codex.
STATUS_DIR = DATA_DIR / "status"
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

# Name -> CLAUDE_CONFIG_DIR, edited via SettingsDialog's "Claude accounts"
# EnvEditor. Falls back to just the account already logged in at ~/.claude
# until the user adds more (see setup_claude_account.sh for provisioning a
# second one with everything but credentials symlinked from this).
DEFAULT_CLAUDE_ACCOUNTS = {"Default": str(HOME / ".claude")}


def populate_claude_account_combo(
    combo: QComboBox, accounts: dict[str, str], current: str | None
) -> None:
    combo.addItem("Default account", None)
    for name, config_dir in accounts.items():
        combo.addItem(name, config_dir)
    index = combo.findData(current) if current else -1
    if index >= 0:
        combo.setCurrentIndex(index)


def codex_models() -> list[dict]:
    """Codex's own model roster, from the CLI's local cache of the models it can see.

    Unlike CLAUDE_MODELS, there's no small stable alias list for Codex - model
    slugs (and their supported reasoning-effort levels) are just whatever the
    Codex CLI itself last fetched and cached, refreshed independently of
    Session Hub. Returns [] if the cache is missing or unreadable (a fresh
    Codex install, or one that's never been run) - callers fall back to a
    plain "Default" choice plus free-text entry.
    """
    try:
        data = json.loads(CODEX_MODELS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    models = [
        model
        for model in data.get("models", [])
        if model.get("visibility") != "hide" and model.get("slug")
    ]
    models.sort(key=lambda model: model.get("priority", 999))
    return models


def populate_codex_model_combo(combo: QComboBox, current: str | None) -> None:
    """Fill an editable model combo from codex_models(), preselecting `current`.

    Editable + NoInsert (not the default InsertAtBottom): a slug this
    machine's cache doesn't know yet (a brand-new release, or another
    machine's cache) must still be typeable and preserved, without
    permanently polluting the dropdown with one-off entries.
    """
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.addItem("Default", None)
    for model in codex_models():
        combo.addItem(model.get("display_name") or model["slug"], model["slug"])
    index = combo.findData(current) if current else -1
    if index >= 0:
        combo.setCurrentIndex(index)
    elif current:
        combo.setCurrentText(current)


def populate_codex_effort_combo(
    combo: QComboBox, model_slug: str | None, current: str | None
) -> None:
    """Fill an editable effort combo with the levels the given model slug supports."""
    combo.clear()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.addItem("Default", None)
    levels = next(
        (
            model.get("supported_reasoning_levels", [])
            for model in codex_models()
            if model["slug"] == model_slug
        ),
        [],
    )
    for level in levels:
        effort = level.get("effort")
        if effort:
            combo.addItem(effort.capitalize(), effort)
    index = combo.findData(current) if current else -1
    if index >= 0:
        combo.setCurrentIndex(index)
    elif current:
        combo.setCurrentText(current)


def codex_combo_value(combo: QComboBox) -> str | None:
    """The selected item's data, or freely-typed text when nothing matches.

    Checked by comparing displayed text to the selected item's own text, not
    just currentIndex() >= 0: setCurrentText() (used by tests, and Qt itself
    on some code paths) leaves currentIndex() pointing at whatever it was
    before when the text doesn't match any item, unlike interactively typing
    into the field and tabbing away (which resets it to -1 under NoInsert).
    """
    index = combo.currentIndex()
    if index >= 0 and combo.itemText(index) == combo.currentText():
        return combo.currentData()
    return combo.currentText().strip() or None


def invalid_codex_model_effort_reason(model: str | None, effort: str | None) -> str | None:
    """None if (model, effort) is launchable; otherwise a user-facing reason
    it is not.

    Row447 third rework: the model/effort combos are editable (see
    populate_codex_model_combo's own docstring - a slug this machine's cache
    doesn't know yet must still be typeable), so a mismatched pair used to
    reach `codex` inside an async tmux child with no visible symptom - it
    just fails to produce a usable session, indistinguishable from "nothing
    happened" the same way the dotted-name bug was.

    Deliberately does NOT reject a model absent from codex_models() outright
    - the cache can be stale/incomplete by design, and rejecting an unknown
    slug would break launching a real, brand-new Codex release this
    machine's cache hasn't fetched yet. It DOES reject an effort level that
    is objectively wrong for a model this machine's cache DOES recognize -
    that combination will not run under any interpretation of the cache,
    known-bad rather than merely unverifiable.
    """
    if not model or not effort:
        return None
    known = next(
        (entry for entry in codex_models() if entry.get("slug") == model), None
    )
    if known is None:
        return None
    supported = {
        level.get("effort")
        for level in known.get("supported_reasoning_levels", [])
        if level.get("effort")
    }
    if effort in supported:
        return None
    names = ", ".join(sorted(supported)) or "none"
    return (
        f"Codex model {model!r} does not support reasoning effort {effort!r} "
        f"(supported: {names})."
    )


# Shared session-table column set: SessionHub's main listview and
# ManageGroupDialog both render from this (see SessionHub.populate_session_table)
# so their common columns are defined once, in one order.
SESSION_TABLE_COLUMNS = (
    "Agent", "Model", "Name", "Working directory", "Last updated", "Session ID",
)
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
    "CLAUDE_CONFIG_DIR": {
        "kind": "text",
        "placeholder": "~/.claude-2",
        "description": (
            "Which Claude Code account this launches as (its own credentials, "
            "usage limits, login). See Settings → Claude accounts and "
            "setup_claude_account.sh for provisioning a second one."
        ),
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


_TMUX_NAME_UNSAFE = re.compile(r"[.:]")


def sanitize_tmux_session_name(name: str) -> str:
    """Replace characters tmux treats as target-spec separators ('.' between
    window/pane, ':' between session/window) with '_'.

    Passed a name containing either, `tmux new-session -s` silently CREATES
    the session under the substituted name instead of erroring - so a caller
    that goes on to use the original, unsanitized name for `has-session`/
    `attach` (as tmux_group_launch_command's script does for every launch,
    not just the one that created the session) targets a name that was never
    actually created. `attach` then fails with "can't find session", the
    surrounding `exec` fails, and the launched agent ends up running headless
    in a tmux session no window ever attaches to - reproduced live with a
    Codex model slug like "gpt-5.6-luna": every dotted model name silently
    became a no-visible-effect "New" click. Applying the same substitution
    tmux performs BEFORE we ever call it keeps the name we use, the name tmux
    actually assigns, and the name stored as this session's address (Codex
    has no --name; the tmux session name IS it) identical.
    """
    return _TMUX_NAME_UNSAFE.sub("_", name)


def tmux_exact_target(name: str) -> str:
    """A tmux `-t` target spec that matches `name` EXACTLY, never as a prefix.

    Real isolated-tmux control (row447 second rework): with only session
    `foo2` live, `tmux has-session -t foo` exits 0 - tmux's default target
    resolution accepts an unambiguous PREFIX match, so a stale/wrong name
    can still see, stop, inspect, rename or attach to a live session it
    only starts with. The `=` prefix on a target's session-name component
    (tmux's own exact-match syntax) disables that: `-t =foo` does NOT match
    `foo2`. Every `-t` argument built from a canonicalized name must go
    through this, or canonicalizing the name alone still leaves target
    resolution fuzzy.
    """
    return f"={name}"


def suggest_session_name(
    directory: Path | None, model_alias: str | None, existing_names: set[str]
) -> str:
    """`<dirname>-<model>` for a model's first row in a group, `-2`/`-3`/...
    for further rows of the same model - a starting point only, since the
    name field stays freely editable in the dialogs that call this.

    Pre-sanitized for tmux (see sanitize_tmux_session_name) so a dotted model
    slug (routine for Codex, e.g. "gpt-5.6-luna") never even appears as a
    suggestion that would later mismatch the real tmux session name.
    """
    base = (directory.name if directory else "") or "session"
    if model_alias:
        base = f"{base}-{model_alias}"
    base = sanitize_tmux_session_name(base)
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
        scope: str = "this session",
        show_tmux: bool = False,
        tmux_override: bool | None = None,
        provider: str = "Claude",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session launch options")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Launch options for “{session_title}”. These override the global "
            f"settings for {scope} only."
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
        self.codex_model_combo: QComboBox | None = None
        self.codex_effort_combo: QComboBox | None = None
        if provider == "Codex":
            # Codex has no ANTHROPIC_MODEL-equivalent env var (its model is a
            # plain -m/--model argv, see effective_model/terminal_command),
            # so it gets its own fields here instead of living in the env tab.
            model_row = QHBoxLayout()
            model_row.addWidget(QLabel("Model:"))
            self.codex_model_combo = QComboBox()
            populate_codex_model_combo(self.codex_model_combo, model)
            model_row.addWidget(self.codex_model_combo)
            layout.addLayout(model_row)
            effort_row = QHBoxLayout()
            effort_row.addWidget(QLabel("Effort:"))
            self.codex_effort_combo = QComboBox()
            populate_codex_effort_combo(self.codex_effort_combo, model, reasoning_effort)
            effort_row.addWidget(self.codex_effort_combo)
            layout.addLayout(effort_row)
            self.codex_model_combo.currentIndexChanged.connect(
                lambda: populate_codex_effort_combo(
                    self.codex_effort_combo, codex_combo_value(self.codex_model_combo), None
                )
            )
        self.editor = LaunchOptionsEditor(env_overrides, flag_overrides)
        layout.addWidget(self.editor)
        self.tmux_checkbox: QCheckBox | None = None
        if show_tmux:
            self.tmux_checkbox = QCheckBox("Launch in tmux")
            self.tmux_checkbox.setTristate(True)
            self.tmux_checkbox.setToolTip(
                "Relaunches/resumes this session detached inside a tmux "
                "session (named to match its --name flag, set in the CLI "
                "flags tab above), with a terminal attached to it. Requires "
                "tmux and gnome-terminal, and a --name flag set for this "
                "session.\n\n"
                "Three states: checked = always tmux, unchecked = never "
                "tmux, dashed/partial = follow the global \"Launch every "
                "session in tmux\" setting."
            )
            self.tmux_checkbox.setCheckState(
                Qt.CheckState.PartiallyChecked
                if tmux_override is None
                else Qt.CheckState.Checked
                if tmux_override
                else Qt.CheckState.Unchecked
            )
            layout.addWidget(self.tmux_checkbox)
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

    def tmux(self) -> bool | None:
        if self.tmux_checkbox is None:
            return None
        state = self.tmux_checkbox.checkState()
        if state == Qt.CheckState.PartiallyChecked:
            return None
        return state == Qt.CheckState.Checked

    def model(self) -> str | None:
        return codex_combo_value(self.codex_model_combo) if self.codex_model_combo else None

    def reasoning_effort(self) -> str | None:
        return codex_combo_value(self.codex_effort_combo) if self.codex_effort_combo else None

    def accept(self) -> None:
        if self.codex_model_combo is not None:
            reason = invalid_codex_model_effort_reason(self.model(), self.reasoning_effort())
            if reason:
                QMessageBox.warning(self, "Unsupported model/effort", reason)
                return
        super().accept()


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
    count: int | None = None  # non-percent windows, e.g. banked reset credits


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


# task-2142: chrome patterns confirmed against live `tmux capture-pane -p` dumps of a
# real running Claude Code session (VAMP-worker1) AND a real running Codex session
# (VAMP-reviewer), both 2026-08-29 -- see the commit message and the test fixtures for
# the exact samples. Structural (rule-character-dominant lines, blank lines, bare
# prompt carets, spinner/status-line shapes) rather than provider-specific text, so the
# same rules cover both without needing per-provider special-casing; the phrase/
# substring lists are the only provider-specific literals, all drawn from real captures.
_PANE_RULE_CHARS = set("─━═╌╍╾╼┄┅┈┉")
_PANE_BARE_PROMPT_RE = re.compile(r"^[❯>▌›]\s*$")
# The spinner glyph cycles through several frames (✻, ∴, ✢, ✽, ·, braille dots, ...);
# matching the shape -- a short leading glyph, a gerund ending in an ellipsis, then a
# parenthesized "(Nm Ns · ... tokens)" stat block -- is more robust than an exhaustive
# glyph whitelist, which a future spinner frame would silently fall through.
_PANE_SPINNER_STATUS_RE = re.compile(r"^\S{1,2}\s+\S+…\s*\(.*\)$")
_PANE_RATING_LINE_RE = re.compile(r"^(\d:\s+\S+\s*){2,}$")
_PANE_CONTEXT_FOOTER_RE = re.compile(r"context\s+\d+%", re.IGNORECASE)
_PANE_FOOTER_PHRASES = (
    "esc to interrupt", "shift+tab to cycle", "bypass permissions",
    "for shortcuts", "ctrl+c to interrupt", "ctrl+c to exit", "to interrupt",
)
_PANE_KNOWN_CHROME_SUBSTRINGS = (
    "how is claude doing this session?",
    "ask codex to do anything",
)


def _is_pane_border_rule_line(stripped: str) -> bool:
    """A rule/separator line: mostly box-drawing rule characters, with at most a
    short embedded label (a session name, or Codex's "Worked for Ns" caption).
    A real content line is never dominated by these characters."""
    rule_count = sum(1 for c in stripped if c in _PANE_RULE_CHARS)
    if rule_count < 3:
        return False
    label = "".join(c for c in stripped if c not in _PANE_RULE_CHARS).strip()
    return len(label) <= 40 and rule_count >= len(stripped) * 0.5


def _is_pane_chrome_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    # An OSC8 hyperlink's visible remnant text (e.g. a trailing "/rc") renders on its
    # own line in a plain `-p` capture; real content is never this short on its own.
    if len(stripped) <= 3:
        return True
    if _is_pane_border_rule_line(stripped):
        return True
    if _PANE_BARE_PROMPT_RE.match(stripped):
        return True
    if _PANE_SPINNER_STATUS_RE.match(stripped):
        return True
    if _PANE_RATING_LINE_RE.match(stripped):
        return True
    if _PANE_CONTEXT_FOOTER_RE.search(stripped):
        return True
    lowered = stripped.lower()
    if any(p in lowered for p in _PANE_FOOTER_PHRASES):
        return True
    if any(s in lowered for s in _PANE_KNOWN_CHROME_SUBSTRINGS):
        return True
    return False


def extract_last_meaningful_block(raw_pane_text: str) -> str:
    """The latest meaningful terminal-output block from a raw `tmux capture-pane -p`
    dump: strips Claude/Codex prompt borders, footers, spinner-status and blank
    chrome, then returns the last contiguous run of remaining lines (reading from the
    bottom up, stopping at the first post-filter gap) joined by newlines. "" for a
    pane with no meaningful content at all (fresh/empty session, chrome only).
    """
    lines = strip_terminal_codes(raw_pane_text).split("\n")
    block: list[str] = []
    started = False
    for line in reversed(lines):
        if _is_pane_chrome_line(line):
            if started:
                break
            continue
        block.append(line.rstrip())
        started = True
    block.reverse()
    return "\n".join(block)


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


def _codex_window_name(duration: int | None, fallback: str) -> str:
    return (
        f"{duration // 60}-hour"
        if duration and duration < 1440
        else "Weekly" if duration else fallback
    )


def _codex_build_window(
    window: dict | None, fallback: str, name_prefix: str = ""
) -> "UsageWindow | None":
    if not window:
        return None
    duration = window.get("windowDurationMins")
    name = _codex_window_name(duration, fallback)
    return UsageWindow(
        f"{name_prefix} {name}" if name_prefix else name,
        max(0, min(100, int(window.get("usedPercent", 0)))),
        format_reset_timestamp(window.get("resetsAt")),
        window_minutes=duration,
        reset_epoch=window.get("resetsAt"),
    )


def read_codex_usage(timeout: float = 12.0) -> list[UsageWindow | None]:
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
            result = response.get("result", {})
            snapshot = result.get("rateLimits", {})
            by_limit_id = result.get("rateLimitsByLimitId") or {}
            credits = result.get("rateLimitResetCredits") or {}

            # Three row slots, matching Claude's row count so the two panels
            # stay the same height: account weekly, then either the
            # account's own 5-hour window or (when the plan reports
            # per-model limits, e.g. GPT-5.3-Codex-Spark) that model's
            # 5-hour and weekly windows. Banked reset credits aren't a
            # window — they're folded into the header text instead of
            # taking a fourth row.
            windows: list[UsageWindow | None] = [None, None, None]
            windows[0] = _codex_build_window(snapshot.get("primary"), "5-hour")

            model_entry = next(
                (
                    entry
                    for limit_id, entry in (by_limit_id or {}).items()
                    if limit_id != "codex" and entry.get("limitName")
                ),
                None,
            )
            if model_entry:
                model_name = model_entry["limitName"]
                windows[1] = _codex_build_window(
                    model_entry.get("primary"), "5-hour", model_name
                )
                windows[2] = _codex_build_window(
                    model_entry.get("secondary"), "Weekly", model_name
                )
            else:
                windows[1] = _codex_build_window(snapshot.get("secondary"), "Weekly")

            available_count = credits.get("availableCount")
            banked = (
                UsageWindow("Banked resets", 0, "", count=available_count)
                if available_count is not None
                else None
            )

            if any(windows) or banked:
                return windows + ([banked] if banked else [])
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


METADATA_BACKUP_DIR = DATA_DIR / "backups" / "metadata"
METADATA_BACKUP_RETENTION_DAYS = 30


def backup_metadata_once_per_day() -> None:
    """Copy the current metadata.json into METADATA_BACKUP_DIR, once per
    calendar day, before it gets overwritten - a bad write (or a bug) then
    costs at most a day instead of the whole file with no way back."""
    if not METADATA_PATH.exists():
        return
    METADATA_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = METADATA_BACKUP_DIR / f"metadata-{date.today().isoformat()}.json"
    if not dest.exists():
        shutil.copy2(METADATA_PATH, dest)
    cutoff = time.time() - METADATA_BACKUP_RETENTION_DAYS * 86400
    for old in METADATA_BACKUP_DIR.glob("metadata-*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink()


def write_metadata(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    backup_metadata_once_per_day()
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
                    # A subagent thread (thread_source='subagent') is spawned
                    # BY another Codex session's own turn, not launchable or
                    # resumable on its own - `codex resume <id>` on one exits
                    # immediately. Excluding it here is the only place that
                    # matters: every session picker/linker in the app reads
                    # from this list, so a subagent thread never looks like
                    # an ordinary standalone session to link or launch.
                    "FROM threads WHERE thread_source IS NOT 'subagent' "
                    "ORDER BY updated_at_ms DESC"
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
            # Same subagent-thread exclusion as the sqlite path above, for
            # when CODEX_STATE is missing/unreadable and this raw-rollout
            # scan is the only source of Codex sessions.
            if isinstance(payload.get("source"), dict) and "subagent" in payload["source"]:
                continue
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


def resolve_pending_links(metadata: dict, sessions: list[Session]) -> bool:
    """A cross-provider swap (continue_with_other_agent_for) doesn't know the
    new native session id for Codex/Antigravity until the CLI itself creates
    it - this matches the first same-provider/same-cwd session that shows up
    after the swap started and folds it into the same logical link, so a
    later swap back to that provider finds it as an existing_target instead
    of always starting another fresh session."""
    changed = False
    pending = metadata.setdefault("pending_links", [])
    remaining = []
    now_ms = int(datetime.now().timestamp() * 1000)
    for item in pending:
        if now_ms > int(item.get("expires_ms", now_ms + 1)):
            changed = True
            continue
        candidates = [
            session
            for session in sessions
            if session.provider == item.get("target_provider")
            and session.native_key not in set(item.get("existing_keys", []))
            and session.updated_ms >= int(item.get("started_ms", 0))
            and Path(session.cwd) == Path(item.get("cwd", ""))
        ]
        tmux_name = item.get("target_tmux_name")
        if tmux_name and item.get("target_provider") == "Codex":
            # Provider+cwd+timestamp is not an identity. Session groups
            # deliberately run several Codex rows in the same cwd, so a
            # simultaneous worker launch used to get stolen by whichever
            # pending handoff refreshed first. The running Codex process has
            # its rollout open; bind to that exact tmux row instead.
            tmux_key = codex_tmux_native_key(tmux_name)
            if tmux_key is None:
                remaining.append(item)
                continue
            candidates = [session for session in candidates if session.native_key == tmux_key]
        if not candidates:
            remaining.append(item)
            continue
        # Without an exact tmux identity, ambiguity is a reason to wait, not
        # permission to attach an unrelated same-directory conversation.
        if len(candidates) != 1:
            remaining.append(item)
            continue
        target = candidates[0]
        logical_key = item["logical_key"]
        link = metadata.setdefault("links", {}).setdefault(
            logical_key, {"members": [logical_key], "active": logical_key}
        )
        if target.native_key not in link["members"]:
            link["members"].append(target.native_key)
        link["active"] = target.native_key
        model = item.get("model")
        reasoning_effort = item.get("reasoning_effort")
        if model or reasoning_effort:
            entry = metadata.setdefault("sessions", {}).setdefault(target.native_key, {})
            if target.provider == "Claude":
                if model:
                    entry.setdefault("env", {})["ANTHROPIC_MODEL"] = model
            elif target.provider == "Codex":
                if model:
                    entry["model"] = model
                if reasoning_effort:
                    entry["reasoning_effort"] = reasoning_effort
        changed = True
    metadata["pending_links"] = remaining
    return changed


def rename_group_row_in(metadata: dict, cwd: str, old: str, new: str) -> dict:
    """Rename a saved group row IN PLACE: the row's name, its override_key, the
    override bucket under it, and nothing else.

    There is ONE name per row (user 2026-08-22: *"can we make the internal row
    id also use the display rename?"*). Before this, "Rename" on a group row
    wrote a display override while row["name"] -- the Claude --name, the tmux
    session name and therefore the terminal title (set-titles-string "#S") --
    kept the name the row was created with, so the window bar still read
    `Vampulse-sonnet2` after the table said `VAMP-worker2`. Matching a row to
    its live session goes by session_key first (find_group_member_session), so
    an already-launched row survives the rename; a fresh launch passes the new
    --name.

    `new` is canonicalized (sanitize_tmux_session_name) before any check or
    write - row447 rework: a dedup/collision check run against the raw text
    would miss two different raw names that canonicalize to the same tmux
    identity, and the stored row["name"] must equal what tmux actually calls
    the session or every downstream reader (group_row_status, launch/stop/
    resume, pending Codex links) drifts from it again.
    """
    new = sanitize_tmux_session_name(" ".join(str(new).strip().split()))
    if not new:
        return {"status": "error", "message": "Row name must not be empty"}
    group = metadata.get("groups", {}).get(cwd)
    if not group:
        return {"status": "error", "message": f"No session group for {cwd}"}
    rows = group.get("rows", [])
    row = next((r for r in rows if r["name"] == old), None)
    if not row:
        return {"status": "error", "message": f"No row named {old!r} in this group"}
    if new == old:
        return {"status": "unchanged", "name": new}
    if any(r["name"] == new for r in rows):
        return {"status": "error", "message": f"A row named {new!r} already exists"}
    old_key = row["override_key"]
    new_key = f"group:{cwd}#{new}"
    sessions = metadata.setdefault("sessions", {})
    bucket = sessions.pop(old_key, {})
    bucket["name"] = new
    sessions[new_key] = bucket
    row["name"] = new
    row["override_key"] = new_key

    # A group-row link is keyed by the stable override key. Leaving it under
    # the old key splits the next continuation into a second chain, while
    # stale native-key names keep surfacing the pre-rename label in pickers
    # and metadata. Rename the identity and every transcript in that one
    # logical conversation together.
    links = metadata.setdefault("links", {})
    link_key = old_key if old_key in links else None
    if link_key is None and row.get("session_key"):
        matching_links = [
            logical_key
            for logical_key, link in links.items()
            if row["session_key"] in link.get("members", [])
        ]
        if len(matching_links) == 1:
            link_key = matching_links[0]
    if link_key is not None:
        old_link = links.pop(link_key)
        if new_key in links:
            merged = links[new_key]
            for member in old_link.get("members", []):
                if member not in merged.setdefault("members", []):
                    merged["members"].append(member)
            if old_link.get("active"):
                merged["active"] = old_link["active"]
        else:
            links[new_key] = old_link
    for pending in metadata.get("pending_links", []):
        if pending.get("logical_key") == old_key:
            pending["logical_key"] = new_key
        if pending.get("target_tmux_name") == old:
            pending["target_tmux_name"] = new

    sync_group_row_name(metadata, row)
    return {"status": "renamed", "old": old, "name": new}


def group_row_native_keys(metadata: dict, row: dict) -> set[str]:
    """Native transcript keys belonging to one saved group row."""
    keys = {row.get("session_key", "")}
    override_key = row.get("override_key")
    for logical_key, link in metadata.get("links", {}).items():
        members = set(link.get("members", []))
        if logical_key == override_key or keys.intersection(members):
            keys.update(members)
    return {
        key for key in keys
        if key.startswith(("Claude:", "Codex:", "Antigravity:"))
    }


def sync_group_row_name(metadata: dict, row: dict) -> bool:
    """Make a row rename visible in every metadata identity it owns."""
    name = row.get("name")
    override_key = row.get("override_key")
    if not name or not override_key:
        return False
    changed = False
    sessions = metadata.setdefault("sessions", {})
    for key in {override_key, *group_row_native_keys(metadata, row)}:
        bucket = sessions.setdefault(key, {})
        if bucket.get("name") != name:
            bucket["name"] = name
            changed = True
    return changed


def sync_group_row_names(metadata: dict) -> bool:
    """Upgrade old rename metadata to the row's single current name."""
    changed = False
    for group in metadata.get("groups", {}).values():
        for row in group.get("rows", []):
            if sync_group_row_name(metadata, row):
                changed = True
    return changed


def rename_tmux_session(old: str, new: str) -> bool:
    """`tmux rename-session old new` if a session called `old` exists; the
    terminal title follows on its own (set-titles-string "#S"). False when
    tmux is absent or no such session -- nothing to rename is not an error.

    Both names are canonicalized through sanitize_tmux_session_name first -
    a caller that still holds a pre-fix unsafe stored name (row447 rework:
    legacy metadata, or a value that skipped an upstream choke point) would
    otherwise target `old`/`new` literals tmux itself never used, the same
    has-session/attach target-spec mismatch tmux_group_launch_command's own
    fix addresses for launch."""
    old = sanitize_tmux_session_name(old)
    new = sanitize_tmux_session_name(new)
    tmux = shutil.which("tmux")
    if not tmux or old == new:
        return False
    try:
        has = subprocess.run(
            [tmux, "has-session", "-t", tmux_exact_target(old)], capture_output=True, timeout=5
        )
        if has.returncode != 0:
            return False
        done = subprocess.run([tmux, "rename-session", "-t", tmux_exact_target(old), new],
                              capture_output=True, timeout=5)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def reconcile_tmux_rename(
    old_name: str, new_name: str, live_names: frozenset[str] | None = None
) -> dict:
    """Rename the live tmux session `old_name` to `new_name` and report what
    happened, instead of leaving the caller with rename_tmux_session's bare
    True/False - a metadata rename that already committed while the tmux
    rename silently failed is exactly the split identity row447 rework
    reported live (Codex renamed to "Music Download", tmux stayed "projects").

    Both names are canonicalized (rename_tmux_session does this too, but the
    collision check below needs the canonical form up front). Refuses with
    an `error` message - and touches nothing - when `new_name` already names
    a DIFFERENT live tmux session: tmux's own `rename-session` would fail
    for the same reason, but this gives a caller a message to show instead
    of a bare False indistinguishable from "nothing needed renaming".

    A caller should run this BEFORE writing its own metadata, so an `error`
    result leaves the two identities exactly as consistent as they were
    before the rename was attempted (atomic, not "renamed the label, tmux
    disagrees").
    """
    old_name = sanitize_tmux_session_name(old_name)
    new_name = sanitize_tmux_session_name(new_name)
    if old_name == new_name:
        return {"tmux_renamed": False, "error": None}
    names = tmux_live_session_names() if live_names is None else live_names
    if new_name in names:
        return {
            "tmux_renamed": False,
            "error": f"A tmux session named {new_name!r} already exists.",
        }
    if old_name not in names:
        # Nothing live under the old name (not yet launched, or already
        # stopped) - no tmux rename needed, and none attempted.
        return {"tmux_renamed": False, "error": None}
    if rename_tmux_session(old_name, new_name):
        return {"tmux_renamed": True, "error": None}
    return {
        "tmux_renamed": False,
        "error": f"Renaming the live tmux session {old_name!r} to {new_name!r} failed.",
    }


def find_group_member_session(
    row: dict,
    cwd: str,
    sessions: list[Session],
    exclude_keys: frozenset[str] = frozenset(),
    linked_session_keys: frozenset[str] = frozenset(),
) -> Session | None:
    """The live session a saved group row refers to, if launched.

    Checked in order:
    1. `row["session_key"]` - the native key discover_sessions last saw this row
       matched to, kept alive across a /clear or manual relink because the active
       session's `linked_keys` (from metadata["links"]) still contains it. This is
       what lets a row survive a restart that has no agent-name record at all.
       Not cwd-gated: an exact session_key match (or linked_keys membership) is
       already unambiguous identity, and a long-running agent's own cwd can
       legitimately drift (cd into a worktree, a subdirectory, etc.) without
       that meaning the row now belongs to a different session.
    1b. If step 1 found nothing AND `session_key` is itself a member of some
       link (`linked_session_keys`, from every metadata["links"] members
       list), the row's identity is under link management, not a fresh
       row - resolve_link_active having returned None for that link (every
       member gone) means genuinely orphaned. Fail closed here rather than
       falling through to step 2, or a same-cwd/same-agent-name sibling
       that was never part of this row's link gets silently resumed in its
       place.
    2. `--name` (Session.agent_name, parsed from the transcript's own agent-name
       record - see _scan_claude_file) plus cwd - the bootstrap match, used before
       any session_key has been recorded (a row's first launch). Claude-only:
       Codex has no equivalent flag to tag a name onto a fresh launch.
    3. Codex only, and only for a row this session itself just launched
       (`row["codex_pending_since"]`, stamped by launch_group_row): the
       newest live Codex session in this cwd updated since then, not already
       claimed by a sibling row this same pass (`exclude_keys`). Codex has no
       exact identity for a brand-new session the way --name/--resume give
       Claude one, so this is a scoped guess - same risk class
       adopt_untracked_sessions already accepts for the analogous
       no-identifying-flags Claude case, narrowed here to only ever fire for
       a row this session actually just launched, and only until it matches
       once (session_key then sticks).
    """
    provider = row.get("provider", "Claude")
    session_key = row.get("session_key")
    pending_since = row.get("codex_pending_since") if provider == "Codex" else None
    if pending_since:
        candidates = [
            session
            for session in sessions
            if session.provider == "Codex"
            and session.cwd == cwd
            and session.updated_ms >= pending_since
            and session.native_key not in exclude_keys
        ]
        # A pending launch explicitly asks for a NEW transcript. It must win
        # over the row's historical session_key; otherwise the old file is
        # found on disk first forever and the new conversation can never be
        # adopted. More than one candidate is ambiguous, not "pick newest".
        return candidates[0] if len(candidates) == 1 else None
    if session_key:
        match = next(
            (
                session
                for session in sessions
                if (session.provider == provider and session.native_key == session_key)
                # A cross-provider link (e.g. Claude row linked to a Codex
                # continuation) makes the merged session's own .provider
                # differ from the row's original provider - linked_keys
                # membership is already a strong identity match, so it
                # must not also require the provider to still agree.
                or session_key in session.linked_keys
            ),
            None,
        )
        if match:
            return match
        if session_key in linked_session_keys:
            return None
    if provider == "Claude":
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
    return None


def all_linked_member_keys(metadata: dict) -> frozenset[str]:
    """Every native key named in ANY link's members list, alive or gone -
    lets find_group_member_session tell a genuinely orphaned link (its
    session_key was link-tracked, every member is now gone) from a row
    that never had a link, so only the latter may fall back to the loose
    name+cwd match."""
    return frozenset(
        key
        for link in metadata.get("links", {}).values()
        for key in link.get("members", [])
    )


def resolve_link_active(link: dict, by_key: dict[str, "Session"]) -> "Session | None":
    """The link's current target: link["active"] if that member still
    exists, else the newest surviving member by updated_ms (native_key
    breaks a tie deterministically). Repairs a link whose recorded active
    member was deleted/trashed instead of leaving the whole link unmatched -
    the reversed(members) insertion-order fallback this replaced picked
    whichever member was linked first, not most recently active."""
    active = by_key.get(link.get("active"))
    if active:
        return active
    candidates = [by_key[key] for key in link.get("members", []) if key in by_key]
    if not candidates:
        return None
    return max(candidates, key=lambda session: (session.updated_ms, session.native_key))


def linked_aware_sessions(sessions: list["Session"], links: dict) -> list["Session"]:
    """`sessions` (a raw, un-collapsed per-provider list) with every
    non-active link member HIDDEN and `.linked_keys` populated on the
    survivor - mirrors discover_sessions's own link-collapse (hidden/
    visible_linked) exactly, and for the same reason: a stale member left
    in the pool matches on its own literal native_key before
    find_group_member_session's `next()` ever reaches the active session's
    linked_keys branch, since an exact self-match doesn't care that a newer
    continuation exists. Only the active member gets linked_keys, never
    every member: setting it on all of them let find_group_member_session's
    next() match whichever one happened to sort first in `sessions`, so a
    cross-provider link matched the stale non-active session instead of the
    one the link actually points at."""
    by_key = {session.native_key: session for session in sessions}
    hidden: set[str] = set()
    active_sessions: list[Session] = []
    for link in links.values():
        members = tuple(link.get("members", []))
        hidden.update(members)
        active = resolve_link_active(link, by_key)
        if active:
            # Mutates the link dict in place (a live reference into
            # metadata["links"], not a copy) so a repaired active member
            # survives past this one call - group_row_candidates persists
            # it. Mirrors discover_sessions's own repair-write below.
            link["active"] = active.native_key
            active.linked_keys = members
            active_sessions.append(active)
    return [
        session for session in sessions if session.native_key not in hidden
    ] + active_sessions


def group_row_candidates(metadata: dict, settings: dict) -> list[Session]:
    """Every enabled-provider live session, link-aware - the pool a saved
    group row's identity must be resolved against. Never call
    claude_sessions()/codex_sessions() directly for a group-row match: a
    same-provider-only, unlinked candidate list finds the row's stored
    (possibly stale) native key literally instead of the link's current
    target, which is exactly the "resume opens an older linked rollout"
    bug - and a row relinked to a different provider than it was saved
    under needs that provider's sessions in the pool at all to match."""
    live: list[Session] = []
    if settings.get("enable_claude", True):
        live += claude_sessions()
    if settings.get("enable_codex", True):
        live += codex_sessions()
    if settings.get("enable_antigravity", True):
        live += antigravity_sessions()
    links = metadata.get("links", {})
    before = {key: link.get("active") for key, link in links.items()}
    candidates = linked_aware_sessions(live, links)
    # A resume/launch invoked directly (CLI/TUI) may be the first call this
    # process makes - it never goes through discover_sessions's own repair
    # write, so linked_aware_sessions's in-place repair above must be
    # persisted here or it re-derives from scratch, silently, every call.
    if any(link.get("active") != before.get(key) for key, link in links.items()):
        write_metadata(metadata)
    return candidates


def resolve_pending_codex_group_rows(metadata: dict, sessions: list[Session]) -> bool:
    """Bind newly launched Codex group rows to their exact tmux transcript.

    Codex has no --name flag, but its process keeps the rollout JSONL open.
    The tmux row therefore provides a stronger identity than cwd/mtime and
    disambiguates simultaneous Codex workers sharing one project directory.
    """
    by_key = {session.native_key: session for session in sessions}
    changed = False
    for group in metadata.get("groups", {}).values():
        if not group.get("tmux"):
            continue
        for row in group.get("rows", []):
            if row.get("provider") != "Codex" or not row.get("codex_pending_since"):
                continue
            native_key = codex_tmux_native_key(row["name"])
            match = by_key.get(native_key)
            if match is None or match.updated_ms < int(row["codex_pending_since"]):
                continue
            row["session_key"] = native_key
            row.pop("codex_pending_since", None)
            changed = True
    return changed


def discover_sessions(metadata: dict) -> list[Session]:
    settings = metadata.get("settings", {})
    sessions = []
    if settings.get("enable_codex", True):
        sessions += codex_sessions()
    if settings.get("enable_claude", True):
        sessions += claude_sessions()
    if settings.get("enable_antigravity", True):
        sessions += antigravity_sessions()
    changed = sync_group_row_names(metadata)
    if resolve_pending_codex_group_rows(metadata, sessions):
        changed = True
    if resolve_pending_links(metadata, sessions):
        changed = True
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
    links_changed = False
    for logical_key, link in metadata.setdefault("links", {}).items():
        members = tuple(link.get("members", []))
        active = resolve_link_active(link, by_key)
        hidden.update(members)
        if not active:
            continue
        # resolve_link_active only SELECTS a repair; persist it here so the
        # next read (a CLI resume with no prior discover_sessions call) sees
        # the corrected member instead of re-deriving the same repair from
        # scratch every time link["active"] itself is never updated.
        if link.get("active") != active.native_key:
            link["active"] = active.native_key
            links_changed = True
        active.logical_key = logical_key
        active.linked_keys = members
        custom = overrides.get(logical_key, {})
        active.title = custom.get("name") or active.title
        active.cwd = custom.get("cwd") or active.cwd
        visible_linked.append(active)
    if links_changed:
        write_metadata(metadata)
    visible = [
        session for session in sessions if session.native_key not in hidden
    ] + visible_linked
    for session in visible:
        custom = overrides.get(session.key, {})
        session.title = custom.get("name") or session.title
        session.cwd = custom.get("cwd") or session.cwd

    # A saved session group (see ManageGroupDialog/LaunchNewGroupSessionsDialog)
    # collapses its launched member sessions into one representative row,
    # the same way a cross-agent link does above - members stay fully
    # intact and reappear on their own once removed from the group.
    group_hidden = set()
    group_pseudo_sessions = []
    groups_changed = False
    for cwd, group in metadata.setdefault("groups", {}).items():
        max_updated = 0
        provider_counts: dict[str, int] = {}
        for row in group.get("rows", []):
            provider_counts[row.get("provider", "Claude")] = (
                provider_counts.get(row.get("provider", "Claude"), 0) + 1
            )
            match = find_group_member_session(row, cwd, visible, frozenset(group_hidden))
            if match:
                group_hidden.add(match.native_key)
                max_updated = max(max_updated, match.updated_ms)
                if row.get("session_key") != match.native_key:
                    row["session_key"] = match.native_key
                    groups_changed = True
                if row.pop("codex_pending_since", None) is not None:
                    groups_changed = True
        display_name = group.get("display_name") or Path(cwd).name or cwd
        # The pseudo-session's own provider is cosmetic only (Agent-column
        # color for the group's summary row) - every real launch/match
        # already goes through each row's own provider, not this one.
        pseudo_provider = max(provider_counts, key=provider_counts.get) if provider_counts else "Claude"
        group_pseudo_sessions.append(
            Session(pseudo_provider, f"group:{cwd}", display_name, cwd, cwd, max_updated, Path(cwd))
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


def _terminal_windows() -> list[tuple[str, str]]:
    """(window_id, title) for every currently open gnome-terminal window.

    wmctrl -l's title-only view can't tell a terminal window from Session
    Hub's own main window - if a Claude/Codex session gets renamed to match
    the launcher's own "Session Hub" window title (exactly what happened for
    this project's own dev session), a plain substring-on-title match picks
    whichever window wmctrl lists first, which may well be the launcher
    itself rather than the terminal. -lx adds a WM_CLASS column;
    gnome-terminal's is always "gnome-terminal-server.Gnome-terminal", never
    the launcher's own - filtering on it, then activating by window ID
    rather than by title, is the only way to guarantee the right window.
    """
    wmctrl = shutil.which("wmctrl")
    if not wmctrl:
        return []
    try:
        result = subprocess.run([wmctrl, "-lx"], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return []
    windows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and "gnome-terminal" in parts[2].lower():
            windows.append((parts[0], parts[4]))
    return windows


def window_titled(title: str) -> bool:
    """True if a currently open gnome-terminal window's title contains
    `title` right now.

    A single wmctrl -lx snapshot, not the poll loop focus_window_by_title
    runs - callers use this to decide whether a window to reveal already
    exists versus needs to be opened first.
    """
    return any(title in window_title for _, window_title in _terminal_windows())


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
        for window_id, window_title in _terminal_windows():
            if title in window_title:
                subprocess.run([wmctrl, "-i", "-a", window_id], timeout=1)
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


def prefix_env_command(
    args: list[str], env_overrides: dict[str, str], strip: list[str] | None
) -> list[str]:
    """Prefix `args` with `env` so overrides/strips apply to the exec'd
    process directly, independent of whatever environment the process that
    execs it happens to have.

    Needed for tmux_group_launch_command: Popen's env= kwarg only reaches
    the process Session Hub directly spawns, not a session an
    already-running tmux server creates on that process's behalf.

    No `--` before the command: `env`'s NAME=VALUE parsing already stops at
    the first argument that isn't a NAME=VALUE pair, and (confirmed against
    a real launch failure) not every env build treats `--` as a supported
    separator there - one rejected it outright ("env: '--': No such file or
    directory"), which killed the whole tmux session before it could ever
    attach.
    """
    if not env_overrides and not strip:
        return args
    prefix = ["env"]
    for key in strip or []:
        prefix += ["-u", key]
    for key, value in env_overrides.items():
        prefix.append(f"{key}={value}")
    return [*prefix, *args]


def tmux_group_launch_command(name: str, cwd: str, claude_args: list[str]) -> list[str]:
    """Launch `claude_args` detached inside a tmux session named `name`,
    then open a terminal already attached to it.

    Requested by the VAMPULSE-orchestrator session: peer sessions drive each
    other via injected keystrokes, and gnome-terminal's VTE silently
    discards synthetic XSendEvent keystrokes (confirmed empty-terminal in
    testing), so any no-focus-impact automation has to go through
    `tmux send-keys` instead - which only reaches processes actually running
    inside tmux. The tmux session name must equal the Claude `--name` value
    exactly: external tooling looks a session up by that name, not by PID.

    Unlike pid_capture_command, this does NOT capture the launched claude
    process's PID - it isn't a child of the process this command spawns (tmux
    daemonizes it), so the "echo $$ into a pidfile" trick can't reach it.
    /clear-detection and "already running" tracking are therefore not yet
    wired up for tmux-launched sessions (deliberately out of scope for this
    pass - see SessionHub.launch_group_row_via_tmux).
    """
    name = sanitize_tmux_session_name(name)
    terminal = shutil.which("gnome-terminal")
    if not terminal:
        raise RuntimeError("Launching into tmux currently requires gnome-terminal.")
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError("tmux is not installed.")
    claude_command = shlex.join(claude_args)
    # has-session first, not straight to new-session: `tmux new-session -d
    # -s NAME` FAILS outright ("duplicate session") if a session by that
    # name already exists - and since this used to be chained with `&&`,
    # that failure silently skipped the exec attach step entirely, so
    # nothing visibly happened at all. A name collision is the normal case
    # here, not an edge case: the tmux session name always equals the row's
    # own name, so re-launching (or double-clicking) a row whose earlier
    # tmux session never got torn down hits this every time - attach to
    # whatever is already there instead of erroring.
    # set-titles: tmux ships it OFF, so it never sets the terminal's title and the emulator falls
    # back to its own default -- on a ja_JP desktop that is the literal string 端末, IDENTICALLY for
    # every window. Tooling that resolves a session by window title then maps several windows onto
    # one session and silently loses the rest, which is dangerous rather than merely untidy: it is
    # how a "/compact" keystroke can land in the wrong terminal. `#S` is the tmux session name, which
    # this launcher already forces to equal the row name and the Claude --name, so one string
    # identifies the window, the tmux session and the Claude session.
    # `-g` is a SERVER option, so it does not survive the tmux server dying -- setting it on EVERY
    # launch is deliberate, cheap and idempotent, and beats a ~/.tmux.conf the user has to maintain.
    # focus-events: off by default, and tmux swallows the outer terminal's focus in/out escapes
    # without it -- so Claude Code's AskUserQuestion AFK auto-continue timeout (which only starts
    # once it sees a focus-out) never fires for a tmux-launched session no matter what
    # askUserQuestionTimeout is set to.
    # `--window`: without it, gnome-terminal's D-Bus server folds a launch that lands while another
    # of its windows is still opening into a TAB of that existing window instead of a new one - hit
    # by ctrl-click-launching two group rows together (VAMPULSE-orchestrator + a worker), where the
    # window ended up titled for one row while actually attached to the other's tmux session. The
    # non-tmux path (terminal_command) already forces this for the same reason.
    # `-t "=$2"`, not `-t "$2"`: tmux's default target resolution accepts an
    # unambiguous PREFIX match (real isolated-tmux control, row447 second
    # rework: with only "foo2" live, `has-session -t foo` exits 0) - the `=`
    # prefix forces an exact match, so this script's has-session/attach never
    # silently binds to a DIFFERENT, merely-prefixed session. See
    # tmux_exact_target.
    return [
        "bash",
        "-c",
        '"$1" has-session -t "=$2" 2>/dev/null || "$1" new-session -d -s "$2" -c "$3" "$4";'
        ' "$1" set-option -g set-titles on >/dev/null;'
        ' "$1" set-option -g set-titles-string "#S" >/dev/null;'
        ' "$1" set-option -g focus-events on >/dev/null;'
        ' exec "$5" --window -- "$1" attach -t "=$2"',
        "session-hub",
        tmux,
        name,
        cwd,
        claude_command,
        terminal,
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


def capture_hub_launch(
    pidfile: Path, cwd: str, session_id: str | None, model: str | None = None
) -> None:
    pid = read_pid_capture_file(pidfile)
    if pid is not None:
        record_hub_launch(pid, cwd, session_id, model)


def record_hub_launch(
    pid: int, cwd: str, session_id: str | None, model: str | None = None
) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    tracking_file = PID_DIR / f"{pid}.json"
    payload = {"cwd": cwd, "session_id": session_id}
    if model:
        # Carries the model chosen in the New Session dialog forward to
        # resolve_clear_continuations, which is the first place this brand
        # new session's real native key becomes known - that's where it
        # turns into a durable per-session ANTHROPIC_MODEL override, the
        # same bucket "Edit launch options..." writes to.
        payload["pending_model"] = model
    try:
        tracking_file.write_text(json.dumps(payload))
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


def tmux_live_session_names() -> frozenset[str]:
    """One bounded snapshot of every currently-alive tmux session name.

    A refresh (GUI All Sessions, Running tab, or --sessions-json) used to call
    `tmux_session_alive` once per row via group_row_status/standalone_tmux_status
    and AGAIN per row via session_activity - two `tmux` subprocess spawns per
    row, and All Sessions fans this over every historical session, not just
    live ones. `tmux list-sessions` is one process for the whole refresh; every
    caller now takes this snapshot and does an in-memory membership check
    instead of spawning its own subprocess (see tmux_session_alive's
    `live_names` param).
    """
    tmux = shutil.which("tmux")
    if not tmux:
        return frozenset()
    try:
        result = subprocess.run(
            [tmux, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        # OSError covers the tmux binary vanishing/becoming unexecutable between the
        # shutil.which() resolution above and this spawn, and any other launch-time OS
        # failure (e.g. permission, resource limits) - same fail-closed reading as the
        # "no tmux server running" branch below, not a crash of the whole census.
        return frozenset()
    if result.returncode != 0:
        # Also the correct (and common) reading for "no tmux server running
        # at all" - has-session's per-name equivalent already fails closed.
        return frozenset()
    return frozenset(line for line in result.stdout.splitlines() if line)


def tmux_pane_activity_snapshot() -> dict[str, tuple[str, str]]:
    """One bounded `tmux list-panes -a` call: {session_name: (pane_id, window_activity)}.

    `window_activity` is tmux's own last-output timestamp for the pane's window --
    free (already tracked by the tmux server for every pane, no opt-in option
    needed), so comparing it against a previous snapshot tells us whether a pane's
    content changed since last look without ever calling capture-pane. task-2142's
    "capture only changed panes" requirement is built on this: a session whose
    window_activity is unchanged needs no fresh capture at all.
    """
    tmux = shutil.which("tmux")
    if not tmux:
        return {}
    try:
        result = subprocess.run(
            [tmux, "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{window_activity}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    snapshot: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, pane_id, activity = parts
        # A tmux session can have several windows/panes; the group-row/standalone
        # launch paths always create exactly one, but if more than one somehow
        # exists for a name, keep the FIRST (list-panes' own window/pane order),
        # not the last -- deterministic rather than whichever happened to sort last.
        snapshot.setdefault(name, (pane_id, activity))
    return snapshot


def capture_changed_panes(pane_ids: dict[str, str]) -> dict[str, str]:
    """Batch-capture several panes' text in ONE tmux client invocation.

    `pane_ids` maps an arbitrary key (here, the tmux session name) to its %pane-id.
    tmux's CLI accepts several commands chained by a literal ';' argv token in one
    invocation; injecting a unique `display-message -p <marker>` between each
    `capture-pane -p` gives an unambiguous split point in the combined stdout, so N
    changed panes cost exactly one subprocess spawn -- never N -- matching the
    brief's "batch all changed pane IDs into one bounded tmux client invocation."
    """
    tmux = shutil.which("tmux")
    if not tmux or not pane_ids:
        return {}
    marker = f"@@@SHPANE-{uuid.uuid4().hex}@@@"
    args = [tmux]
    for index, pane_id in enumerate(pane_ids.values()):
        if index > 0:
            args += [";", "display-message", "-p", marker, ";"]
        args += ["capture-pane", "-p", "-t", pane_id]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    parts = result.stdout.split(marker)
    keys = list(pane_ids.keys())
    if len(parts) != len(keys):
        # A capture that failed mid-batch (a pane vanished between the census and
        # this call) desynchronizes the split count -- fail closed to no update
        # rather than mis-attributing one pane's text to another's cache entry.
        return {}
    return dict(zip(keys, parts))


def tmux_session_alive(name: str, live_names: frozenset[str] | None = None) -> bool:
    """Is a tmux session named `name` currently alive.

    Provider-agnostic and correct for any tmux-launched group row - unlike
    session_is_tracked_alive, which only ever recognizes Claude processes
    (see adopt_untracked_sessions). A row's tmux session name always equals
    its own row["name"] (tmux_group_launch_command's own invariant), so this
    needs no session-id matching at all.

    `live_names`, when given, is a tmux_live_session_names() snapshot - pass
    one whenever checking more than one name per refresh so this does a plain
    membership test instead of its own subprocess. Omitted only by isolated
    callers that just want one name's answer.

    `name` is canonicalized here (see rename_tmux_session) so a caller that
    still holds a stored-but-unsafe name (row447 rework: e.g. group_row_status
    passing a legacy row["name"] straight through) is checked against the
    same substituted form `live_names`/the real tmux daemon actually use,
    instead of a literal that was never the session's real name.
    """
    name = sanitize_tmux_session_name(name)
    if live_names is not None:
        return name in live_names
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    try:
        result = subprocess.run(
            [tmux, "has-session", "-t", tmux_exact_target(name)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        # session_activity now calls this from every live-row status lookup
        # (GUI/TUI/JSON alike), not just the Running tab - a wedged tmux
        # server must not be able to hang status refresh itself, so this
        # fails closed to "not alive" the same way a malformed transcript
        # line fails closed to "unknown" rather than raising. OSError covers
        # the same tmux-binary-vanishes-after-shutil.which() window
        # tmux_live_session_names already guards - this isolated spawn had
        # been left with only the timeout half of that fix.
        return False
    return result.returncode == 0


def codex_tmux_native_key(
    name: str,
    *,
    proc_root: Path | None = None,
    sessions_root: Path | None = None,
) -> str | None:
    """Return the exact Codex transcript key open in tmux session `name`.

    The rollout file descriptor is authoritative for both a fresh `codex`
    process (whose argv has no session id) and `codex resume`. Descendant
    traversal also covers tmux panes that launch through a shell wrapper.

    `name` is canonicalized (see rename_tmux_session) so an unsafe stored
    name still resolves to the real tmux session's panes.
    """
    name = sanitize_tmux_session_name(name)
    tmux = shutil.which("tmux")
    if not tmux:
        return None
    try:
        panes = subprocess.run(
            [tmux, "list-panes", "-t", tmux_exact_target(name), "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if panes.returncode != 0:
        return None

    proc_root = proc_root or PROC_ROOT
    sessions_root = (sessions_root or CODEX_SESSIONS).resolve()
    pending = []
    for value in panes.stdout.split():
        try:
            pending.append(int(value))
        except ValueError:
            continue
    seen: set[int] = set()
    session_id_re = re.compile(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
    )
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        children = proc_root / str(pid) / "task" / str(pid) / "children"
        try:
            pending.extend(int(value) for value in children.read_text().split())
        except (OSError, ValueError):
            pass
        fd_dir = proc_root / str(pid) / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.resolve(strict=True)
                target.relative_to(sessions_root)
            except (OSError, ValueError):
                continue
            match = session_id_re.search(target.name)
            if match:
                return f"Codex:{match.group(1)}"
    return None


def stop_tmux_session(name: str) -> None:
    """Kill the tmux session named `name`, if any. A no-op if already gone.

    `name` is canonicalized (see rename_tmux_session) so an unsafe stored
    name still targets the real tmux session instead of being parsed as a
    session:window.pane target-spec and hitting the wrong thing or nothing.
    """
    name = sanitize_tmux_session_name(name)
    tmux = shutil.which("tmux")
    if not tmux:
        return
    subprocess.run(
        [tmux, "kill-session", "-t", tmux_exact_target(name)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def hook_notify_command() -> list[str]:
    """The exact argv session-hub asks a Claude Code hook to run.

    install_status_hooks (writing it into a project's settings.local.json)
    and uninstall_status_hooks (matching it to remove) must agree on this
    string byte-for-byte, so both go through this one function.
    """
    return [sys.executable, str(Path(__file__).resolve()), "--hook-notify"]


def write_session_status(session_id: str, state: str, detail: str = "", reason: str = "") -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATUS_DIR / f"{session_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"state": state, "ts": time.time(), "detail": detail, "reason": reason})
    )
    tmp.replace(path)


def read_session_status(session_id: str) -> dict | None:
    try:
        return json.loads((STATUS_DIR / f"{session_id}.json").read_text())
    except (OSError, ValueError):
        return None


# Notification types that are a genuine blocker on the agent - the ONLY
# reasons a status file may legitimately carry state="needs_input". A record
# with state="needs_input" and a reason outside this set (or no "reason" key
# at all - every file written before this set existed) is not trustworthy
# evidence of a real blocker and must fail closed to Idle; see
# _resolve_claude_state.
_BLOCKING_NOTIFICATION_TYPES = {"permission_prompt", "agent_needs_input"}


def hook_event_to_status(payload: dict) -> tuple[str, str, str] | None:
    """(state, detail, reason) for one hook stdin payload, or None if this
    event doesn't change status - see hook_notify_cli. `reason` is the raw
    notification_type behind a "needs_input" state, "" otherwise."""
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        return "working", "", ""
    if event == "SessionStart":
        # Fires for plain "startup" too, but also for "resume"/"compact"/
        # "clear" - a /compact or a scheduled wakeup resume isn't the agent
        # starting new work, so treating it as "working" produced a false
        # Working badge on sessions that were actually idle. No prompt has
        # been submitted yet either way, so leave status untouched.
        return None
    if event == "Notification":
        notification_type = payload.get("notification_type", "")
        message = str(payload.get("message", ""))
        if notification_type == "idle_prompt":
            # A live agent sitting at its own idle prompt is Idle, never
            # Needs input - there is no actual blocker here.
            return "idle", message, ""
        if notification_type in _BLOCKING_NOTIFICATION_TYPES:
            return "needs_input", message, notification_type
        if notification_type == "agent_completed":
            return "done", message, ""
        return None
    if event == "Stop":
        return "done", str(payload.get("last_assistant_message", "")), ""
    return None


def hook_event_to_status_codex(payload: dict) -> tuple[str, str] | None:
    """(state, detail) for one Codex `notify` payload, or None to ignore it.

    Codex's `notify` fires only for "agent-turn-complete" - no distinct
    needs-input or working signal exists (approval prompts are a separate,
    non-hookable `tui.notifications` mechanism) - so Codex rows only ever
    reach "done" through this, same Done -> Idle clearing as Claude rows.
    """
    if payload.get("type") != "agent-turn-complete":
        return None
    return "done", str(payload.get("last-assistant-message", ""))


_CODEX_TURN_EVENTS = ("task_started", "task_complete", "turn_aborted")
_CODEX_TAIL_BYTES = 65536  # fixed chunk size for every single read the scan
# below issues, walking backward from EOF - the task_started/task_complete/
# turn_aborted markers this reads for are tiny and always the most recent
# thing worth finding, so almost every call resolves off the first chunk.
_CODEX_TAIL_MAX_BYTES = 8 * 1024 * 1024  # documentation-only reference for
# "how far behind EOF is unusually deep", not a correctness ceiling and not
# a read/allocation size - a turn's own tool-output lines can be arbitrarily
# large (a single custom_tool_call has been observed over 1MB, and a whole
# still-open turn's worth was observed over 20MB against a real live
# rollout), so a marker can legitimately sit this far behind EOF. The
# reverse-chunk walk below reads _CODEX_TAIL_BYTES at a time, moving one
# chunk further from EOF each iteration, until it finds a marker or reaches
# the range's floor - unlike an escalating single read, no individual read
# or allocation ever scales with the distance walked or the file's total
# size (round 2 rework: the prior escalating-window version's read size
# itself grew without bound, allocating gigabytes against a >1.6GB rollout
# with no nearby marker). Steady-state cost stays small because the cache
# resumes each call from near the previous read's end, not from byte 0 -
# see _codex_tail_cache and resume_from below.

# path -> (mtime_ns, size, (dev, ino), result, resume_from) at the time it
# was last read. A GUI refresh can ask about the same live Codex session
# twice in one pass (the All Sessions table and the Running tab are separate
# calls within one refresh()) - keying on the file's own mtime/size rather
# than a refresh-cycle counter means a genuinely-unchanged file is never
# reparsed, AND a file that changed between two calls (a turn actually
# advancing mid-refresh) is still read fresh rather than serving a stale
# cached verdict. (dev, ino) is checked too, not just mtime/size: a path
# whose underlying file was REPLACED (rotation) can coincidentally land on
# the same size, and trusting that as "the same file, just grown" would
# delta-scan from a resume point that belongs to a completely different
# file's history.
#
# codex_sessions() lists every thread ever recorded in the Codex state DB
# (no LIMIT), each with its own rollout_path, so a plain dict here grows one
# entry per historical session forever over a long-running GUI process.
# Bounded LRU instead: only the most recently *looked up* paths (i.e. the
# ones actually shown in a refresh) stay cached.
_CODEX_TAIL_CACHE_MAX = 256
_codex_tail_cache: (
    "collections.OrderedDict[str, tuple[int, int, tuple[int, int], "
    "tuple[str, float] | None, int]]"
) = collections.OrderedDict()


def _codex_tail_turn_state(path: Path) -> tuple[str, float] | None:
    """The most recent task_started/task_complete/turn_aborted event_msg
    record for a Codex rollout file, as (kind, epoch_seconds), or None if
    none has ever been found. "task_started" with nothing newer after it is
    durable evidence of an in-progress turn; "task_complete"/"turn_aborted"
    is durable evidence the last turn already ended - see
    hook_event_to_status_codex's own docstring for why Codex's `notify` alone
    can't tell the difference. Cached per path by (mtime, size, (dev, ino)),
    bounded LRU - see _codex_tail_cache.

    A file that GREW since the last lookup, and is confirmed to be the SAME
    underlying file (dev+inode unchanged), is scanned incrementally
    (_codex_tail_turn_state_delta) from that earlier read's resume_from, not
    re-derived from byte 0 or capped at a fixed ceiling - the reported
    false-Done bug was exactly a fixed ceiling silently discarding a
    still-open turn's task_started once later tool output in that SAME turn
    pushed it far enough behind EOF. A marker once found is never lost to
    later unrelated growth; a genuinely newer marker in the new bytes still
    wins; and an escalating search - now uncapped - still finds a marker
    however far behind EOF it sits on a COLD look (app restart, or a rollout
    seen for the first time), since there is no cached resume point to trust
    yet. A same-or-smaller size, or a changed (dev, ino) identity even at an
    unchanged/larger size (rotation), forces the full cold scan rather than
    trusting stale delta bookkeeping.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    identity = (stat.st_dev, stat.st_ino)
    cached = _codex_tail_cache.get(key)
    if (
        cached is not None
        and cached[0] == stat.st_mtime_ns
        and cached[1] == stat.st_size
        and cached[2] == identity
    ):
        _codex_tail_cache.move_to_end(key)
        return cached[3]
    if cached is not None and cached[2] == identity and cached[1] < stat.st_size:
        # Strict growth of the confirmed-same file: resume from where the
        # previous scan actually left off (its resume_from), which may sit
        # a little before its raw old size if that read raced a torn write
        # - see _codex_scan_range_for_turn_marker's resume_from.
        result, resume_from = _codex_tail_turn_state_delta(
            path, cached[4], stat.st_size, cached[3]
        )
    else:
        result, resume_from = _codex_tail_turn_state_scan(path, stat.st_size)
    _codex_tail_cache[key] = (
        stat.st_mtime_ns, stat.st_size, identity, result, resume_from,
    )
    _codex_tail_cache.move_to_end(key)
    if len(_codex_tail_cache) > _CODEX_TAIL_CACHE_MAX:
        _codex_tail_cache.popitem(last=False)
    return result


_CODEX_MAX_MARKER_LINE_BYTES = 4096  # a task_started/task_complete/
# turn_aborted event_msg record is a tiny fixed-shape object (timestamp,
# type, payload.type only) - it never carries a turn's actual tool output,
# so a candidate line longer than this cannot be one. Gates both the
# parser (cheap, avoids json.loads on a multi-megabyte string) and the
# reverse-chunk walk's carried fragment (round-3 rework - see `oversized`
# in _codex_scan_range_for_turn_marker).


def _codex_parse_turn_marker_line(line: bytes) -> tuple[str, float] | None:
    if len(line) > _CODEX_MAX_MARKER_LINE_BYTES:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    # json.loads can validly return a list/str/int/bool/None for a
    # malformed or unrelated line, not just a dict - and even a genuine
    # event_msg record's "payload" key can hold a non-dict value from a
    # payload shape this code doesn't otherwise care about. Both used to
    # crash on the following .get() call.
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind not in _CODEX_TURN_EVENTS:
        return None
    try:
        epoch = datetime.fromisoformat(
            str(record.get("timestamp")).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, AttributeError):
        return None
    return (kind, epoch)


def _codex_scan_range_for_turn_marker(
    path: Path, floor: int, end: int
) -> tuple[tuple[str, float] | None, int]:
    """Reverse fixed-size-chunk scan for the latest task_started/
    task_complete/turn_aborted event_msg record within byte range
    [floor, end) of `path` - walking backward from `end` one
    _CODEX_TAIL_BYTES chunk at a time until a marker is found or `floor` is
    reached. The caller decides what "nothing in this range" means: no
    marker exists at all (the cold/full scan, floor=0), or nothing NEWER
    than an already-known one (the incremental scan, floor=last confirmed
    resume point). Shared by _codex_tail_turn_state_scan and
    _codex_tail_turn_state_delta.

    Every single `read()` call this makes is <= _CODEX_TAIL_BYTES,
    regardless of how far back in the file (or how large the file) the walk
    must go - a round-2 rework fix. The prior version doubled a SINGLE
    read's size on every escalation with no cap, so a cold scan of a large
    rollout with no nearby marker could allocate a read approaching the
    entire scanned range (observed: ~1.68GB).

    A round-3 rework bounds the OTHER growth path: the carried
    `tail_fragment` (a still-open, not-yet-newline-terminated candidate
    line) used to be re-concatenated with every new chunk with no size
    check, so one giant unterminated non-marker record (the >1MB/>20MB
    tool-output case, same as the read-size bug but via string
    concatenation instead of a single read) still grew to the record's
    full size and copied it again on every iteration - O(n^2) for a single
    n-byte line. Markers are tiny fixed-shape records, so a candidate
    fragment longer than `_CODEX_MAX_MARKER_LINE_BYTES` can never become
    one: once crossed, the fragment is dropped (not truncated-and-kept -
    its bytes are worthless either way) and the walk switches to
    `oversized` mode, which finds the boundary newline that starts the
    giant line using only the freshly-read chunk (a `tail_fragment`, by
    construction, never itself contains an embedded newline, so a new one
    can only appear in a fresh chunk) without ever concatenating or
    re-scanning the discarded bytes. Once that boundary is found, normal
    parsing resumes on the (bounded) data before it - the giant line's own
    remaining bytes are discarded, never rejoining `tail_fragment`.

    Also returns `resume_from`: the offset where the LAST (possibly
    incomplete) line within [floor, end) begins, as measured from the FIRST
    (EOF-anchored) chunk only. A write landing between another process's
    stat()/read() calls can leave the file's exact byte count mid-line
    (most likely for the >1MB single-record tool-output case); resuming a
    later scan from the raw byte count would then read only that line's
    tail half, fail to parse it, and silently lose whatever marker it was.
    Resuming from `resume_from` instead re-reads that small trailing
    fragment together with whatever completes it, so the full record is
    seen intact once the write finishes. In the overwhelmingly common case
    (the file's last byte is already a newline) this is just `end`.
    """
    if end <= floor:
        return None, floor
    try:
        handle = path.open("rb")
    except OSError:
        return None, floor
    with handle:
        position = end
        # The still-incomplete line at the START of the most-recently-read
        # (more EOF-ward) chunk - its true start lies further back in the
        # file, so it is prefixed onto the NEXT (earlier) chunk's data
        # rather than parsed on its own. Bounded to
        # _CODEX_MAX_MARKER_LINE_BYTES - beyond that, `oversized` takes
        # over and this is left empty/unused (see docstring).
        tail_fragment = b""
        oversized = False
        resume_from = end
        first_chunk = True
        while True:
            read_size = min(_CODEX_TAIL_BYTES, position - floor)
            position -= read_size
            try:
                handle.seek(position)
                chunk = handle.read(read_size)
            except OSError:
                return None, floor
            if first_chunk:
                last_newline = chunk.rfind(b"\n")
                resume_from = (
                    position if last_newline == -1 else position + last_newline + 1
                )
                first_chunk = False

            if oversized:
                boundary = chunk.rfind(b"\n")
                if boundary == -1:
                    # Still inside the oversized line - nothing worth
                    # parsing or keeping in this chunk; walk further back
                    # without growing any buffer.
                    if position <= floor:
                        return None, resume_from
                    continue
                # `boundary` is the newline that STARTS the oversized line
                # (everything after it, still part of that line, is
                # discarded); everything before it is fresh, normal data.
                combined = chunk[:boundary]
                oversized = False
            else:
                combined = chunk + tail_fragment
                tail_fragment = b""

            if position <= floor:
                lines = combined.split(b"\n")
            else:
                parts = combined.split(b"\n")
                prefix = parts[0]  # may still be incomplete; carried back
                lines = parts[1:]
                if len(prefix) > _CODEX_MAX_MARKER_LINE_BYTES:
                    oversized = True
                else:
                    tail_fragment = prefix
            latest: tuple[str, float] | None = None
            for line in lines:
                marker = _codex_parse_turn_marker_line(line)
                if marker is not None and (latest is None or marker[1] >= latest[1]):
                    latest = marker
            if latest is not None or position <= floor:
                # A chunk that reached the floor with nothing found is a
                # real "no marker here", not a signal to keep walking back -
                # only an empty chunk that could plausibly be hiding a
                # marker just past its edge continues, and it keeps going
                # (no ceiling) until it either finds one or reaches the
                # floor.
                return latest, resume_from


def _codex_tail_turn_state_scan(
    path: Path, size: int
) -> tuple[tuple[str, float] | None, int]:
    return _codex_scan_range_for_turn_marker(path, 0, size)


def _codex_tail_turn_state_delta(
    path: Path, start: int, end: int, prior: tuple[str, float] | None
) -> tuple[tuple[str, float] | None, int]:
    """Only the bytes appended since the last confirmed read (`start` to
    `end`) can contain a marker newer than `prior` - everything below
    `start` was already accounted for by an earlier call. Falls back to
    `prior` rather than None when nothing new is found, so a marker found
    early in a still-open turn survives however much unrelated tool output
    that same turn goes on to emit after it.
    """
    found, resume_from = _codex_scan_range_for_turn_marker(path, start, end)
    return (found if found is not None else prior), resume_from


def _codex_activity(session: "Session", status: dict | None) -> tuple[str, str]:
    """Codex activity verdict: `notify` only ever proves turn completion
    (hook_event_to_status_codex), so "working" has to come from the rollout
    transcript itself rather than from mere process existence - a completed
    turn with no evidence of a newer one becomes "done", never blank.
    """
    turn = _codex_tail_turn_state(session.path)
    if turn and turn[0] == "task_started" and turn[1] >= (status or {}).get("ts", 0):
        return "working", ""
    if status:
        if (
            status.get("state") == "working"
            and turn is not None
            and turn[0] in ("task_complete", "turn_aborted")
            and turn[1] > status.get("ts", 0)
        ):
            # The status file is stale: it was last written mid-turn
            # ("working") but the transcript proves that turn has since
            # ended (notify hook never fired for the completion event, or
            # its write raced/lost). Without this a session gets stuck
            # showing Working forever once its rollout moves on.
            return "done", ""
        return status.get("state", "unknown"), status.get("detail", "")
    if turn:
        # A turn already finished in the transcript before session-hub's own
        # notify hook ever wrote a status file for it (hook installed after
        # the turn started, or a write that raced/lost) - still don't blank it.
        return "done", ""
    return "unknown", ""


def session_activity(
    session: "Session",
    *,
    tmux_enabled: bool = False,
    tmux_name: str | None = None,
    live_names: frozenset[str] | None = None,
) -> tuple[str, str]:
    """(state, detail) activity verdict for `session`: one of
    working/needs_input/done/idle/unknown. The one function every
    presentation layer showing a session's CURRENT activity - the GUI's
    Running tab and All Sessions table, the TUI's equivalents (via
    --sessions-json, which calls this), and --sessions-json/CLI directly -
    computes it through, so no provider branch can diverge or silently
    return blank (see status_pipeline_plan.md's contract).

    Liveness is a separate fact from activity (also per that contract) and is
    checked here only to keep transcript parsing bounded to rows that are
    actually running: `session_is_tracked_alive` is Claude's own best-effort
    PID tracking, and a tmux-launched row never gets PID-tracked at launch
    (see tmux_group_launch_command) - a caller that already knows this
    session's tmux identity (group_row_status/standalone_tmux_status) passes
    it so a live tmux-launched Codex/Claude/Antigravity session isn't
    wrongly treated as dead. With neither signal, a session reads as not
    live and this returns "unknown" without touching its transcript at all -
    stale status files and stopped processes must not show as live activity.
    """
    if session.session_id.startswith("group:"):
        return "unknown", ""
    live = session_is_tracked_alive(session) or (
        tmux_enabled and tmux_name is not None and tmux_session_alive(tmux_name, live_names)
    )
    if not live:
        return "unknown", ""
    status = read_session_status(session.session_id)
    if session.provider == "Codex":
        return _codex_activity(session, status)
    if not status:
        # Live Claude session, no status file yet (just launched, no hook
        # event has fired) - "no submitted work" is Idle, not Unknown.
        return "idle", ""
    return _resolve_claude_state(status), status.get("detail", "")


def _resolve_claude_state(status: dict) -> str:
    """The status file's state, with a needs_input record lacking an
    explicit blocking reason failed closed to idle - see
    _BLOCKING_NOTIFICATION_TYPES. Covers both a legacy record written before
    "reason" existed (no key at all) and any other non-blocking reason."""
    state = status.get("state", "unknown")
    if state == "needs_input" and status.get("reason") not in _BLOCKING_NOTIFICATION_TYPES:
        return "idle"
    return state


ACTIVITY_LABELS = {
    "working": ("Working", "#5aa9ff"),
    "needs_input": ("Needs input", "#d9534f"),
    "done": ("Done", "#d69e2e"),
    "idle": ("Idle", "#888888"),
}


def activity_label(state: str | None) -> tuple[str, str]:
    """(label, color) for one activity state - see ACTIVITY_LABELS. Shared by
    every presentation layer so "Working"/"Needs input"/etc. render with one
    spelling and color everywhere, not a per-widget copy that can drift."""
    return ACTIVITY_LABELS.get(state, ("", "#888888"))


def hook_notify_cli() -> int:
    """--hook-notify: the command Claude Code itself runs as a hook.

    Reads the hook's stdin JSON, writes STATUS_DIR/<session_id>.json. Never
    raises - a hook that errors interrupts the very session it's reporting
    on, so malformed input is silently ignored rather than surfaced.
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    session_id = payload.get("session_id")
    if not session_id:
        return 0
    mapped = hook_event_to_status(payload)
    if mapped is None:
        return 0
    write_session_status(session_id, *mapped)
    return 0


def hook_notify_codex_cli(argv: list[str]) -> int:
    """--hook-notify-codex: the command Codex's `notify` config runs.

    Codex appends its JSON payload as one extra argv element after the
    configured command (third-party captures confirm this - OpenAI's own
    docs don't spell out the exact argv shape), so the payload is always
    the LAST argument regardless of how many flags precede it. Never
    raises, same reasoning as hook_notify_cli.
    """
    try:
        payload = json.loads(argv[-1])
    except (IndexError, ValueError):
        return 0
    thread_id = payload.get("thread-id")
    if not thread_id:
        return 0
    mapped = hook_event_to_status_codex(payload)
    if mapped is None:
        return 0
    write_session_status(thread_id, *mapped)
    return 0


_STATUS_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop")


def install_status_hooks(project_dir: Path) -> None:
    """Merge session-hub's status hooks into <project_dir>/.claude/settings.local.json.

    Merges rather than overwrites, mirroring Claude Code's own settings-merge
    semantics - any hooks the user already has there, for these events or
    others, are preserved untouched.
    """
    settings_path = project_dir / ".claude" / "settings.local.json"
    try:
        data = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    hooks = data.setdefault("hooks", {})
    command = shlex.join(hook_notify_command())
    for event in _STATUS_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        already = any(
            h.get("command") == command for entry in entries for h in entry.get("hooks", [])
        )
        if not already:
            entries.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def uninstall_status_hooks(project_dir: Path) -> None:
    """Inverse of install_status_hooks: strips only entries whose command
    matches ours, leaves everything else in the file alone."""
    settings_path = project_dir / ".claude" / "settings.local.json"
    if not settings_path.is_file():
        return
    try:
        data = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        return
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    command = shlex.join(hook_notify_command())
    changed = False
    for event in list(hooks.keys()):
        entries = hooks.get(event) or []
        kept = [
            entry for entry in entries
            if not any(h.get("command") == command for h in entry.get("hooks", []))
        ]
        if len(kept) != len(entries):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    if changed:
        settings_path.write_text(json.dumps(data, indent=2) + "\n")


def codex_notify_command() -> list[str]:
    """The exact argv session-hub asks Codex's `notify` config to run.

    install_status_hooks_codex (writing it into ~/.codex/config.toml) and
    uninstall_status_hooks_codex (matching it to remove) must agree on
    this byte-for-byte, so both go through this one function - same
    pattern as hook_notify_command for the Claude side.
    """
    return [sys.executable, str(Path(__file__).resolve()), "--hook-notify-codex"]


def _read_codex_notify() -> list[str] | None:
    try:
        with CODEX_CONFIG.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("notify")
    return value if isinstance(value, list) else None


def install_status_hooks_codex() -> bool:
    """Set ~/.codex/config.toml's `notify` key to session-hub's own command.

    Unlike Claude's hooks, Codex's `notify` is a single slot (not a
    mergeable list) and user-level only (not per-project) - so this only
    ever writes if the key is unset or already ours. Returns False without
    changing anything if the user has their own notify command configured;
    callers should warn rather than silently overwrite it. Edits only the
    one `notify = [...]` line via a targeted text patch, leaving the rest
    of the user's config.toml untouched - session-hub has no TOML-writer
    dependency to round-trip the whole file with.
    """
    ours = codex_notify_command()
    existing = _read_codex_notify()
    if existing is not None and existing != ours:
        return False
    line = f"notify = {json.dumps(ours)}"
    if not CODEX_CONFIG.is_file():
        CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CODEX_CONFIG.write_text(line + "\n")
        return True
    text = CODEX_CONFIG.read_text()
    if re.search(r"(?m)^notify\s*=.*$", text):
        text = re.sub(r"(?m)^notify\s*=.*$", line, text, count=1)
    else:
        text = text.rstrip("\n")
        text = (text + "\n" if text else "") + line + "\n"
    CODEX_CONFIG.write_text(text)
    return True


def uninstall_status_hooks_codex() -> None:
    """Inverse of install_status_hooks_codex: removes the `notify` line
    only if it's still exactly session-hub's own command."""
    if _read_codex_notify() != codex_notify_command():
        return
    if not CODEX_CONFIG.is_file():
        return
    text = re.sub(r"(?m)^notify\s*=.*\n?", "", CODEX_CONFIG.read_text(), count=1)
    CODEX_CONFIG.write_text(text)


def group_row_status(
    row: dict,
    match: "Session | None",
    tmux_enabled: bool,
    live_names: frozenset[str] | None = None,
) -> str:
    """"Running" or "Stopped" for one group row - the only two states shown anywhere.

    tmux-enabled rows trust tmux_session_alive outright (provider-agnostic,
    authoritative). Non-tmux rows fall back to the older PID-tracking signal,
    which only ever works for Claude - a known, pre-existing limitation this
    doesn't attempt to fix. Pass a tmux_live_session_names() snapshot as
    `live_names` when calling this for more than one row per refresh.
    """
    if tmux_enabled:
        return "Running" if tmux_session_alive(row["name"], live_names) else "Stopped"
    return "Running" if match and session_is_tracked_alive(match) else "Stopped"


def standalone_tmux_status(
    session: "Session",
    overrides: dict,
    settings: dict,
    live_names: frozenset[str] | None = None,
) -> tuple[bool, str | None, str | None]:
    """(tmux_enabled, tmux_name, status) for one non-group session.

    Mirrors SessionHub.resume_session's own tmux_name computation
    ((--name flag override) or (Hub rename) or (transcript title)) so
    tmux_session_alive sees the exact session a resume would attach to.
    status is None when tmux launching isn't enabled for this session -
    there is no tmux session to check. Pass a tmux_live_session_names()
    snapshot as `live_names` when calling this for more than one session per
    refresh.
    """
    explicit = overrides.get("tmux")
    tmux_enabled = explicit if explicit is not None else settings.get("launch_in_tmux", False)
    if not tmux_enabled:
        return False, None, None
    name = (overrides.get("flags") or {}).get("--name") or overrides.get("name") or session.title
    return True, name, ("Running" if tmux_session_alive(name, live_names) else "Stopped")


def parse_claude_cmdline_identity(cmdline: str) -> tuple[str | None, str | None]:
    """(--resume SESSION_ID, --name NAME) explicitly present in a claude argv, if any.

    `cmdline` is /proc's NUL-joined argv. Reading these straight off the
    process's own arguments gives an exact identity instead of a guess -
    see adopt_untracked_sessions, which needs this to disambiguate multiple
    untracked processes sharing one cwd (every member of a tmux-launched
    session group does, since none of them get PID-tracked at launch time -
    see tmux_group_launch_command).
    """
    parts = cmdline.split("\x00")
    resume_id = None
    name = None
    for index, part in enumerate(parts):
        if part == "--resume" and index + 1 < len(parts) and parts[index + 1]:
            resume_id = parts[index + 1]
        elif part == "--name" and index + 1 < len(parts) and parts[index + 1]:
            name = parts[index + 1]
    return resume_id, name


def find_untracked_claude_pids() -> list[tuple[int, str, str | None, str | None]]:
    """(pid, cwd, resume_id, name) for live `claude` CLI processes not yet tracked.

    Best-effort /proc scan: matches any process whose cmdline mentions "claude"
    (covers both a direct binary and a node-shebang-wrapped script) and whose
    cwd we can resolve, excluding PIDs PID_DIR already has a tracking file for.
    Existing tracking (record_hub_launch, written at Session Hub's own launch
    time) already survives Session Hub being closed and reopened - it's a plain
    file under PID_DIR, not in-memory state - so this only needs to cover
    sessions that were never launched *through* Session Hub in the first place
    (a `claude` typed directly into a terminal, one running from before this
    tracking feature existed, a tmux-launched group member - see
    tmux_group_launch_command - ...). See adopt_untracked_sessions.
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
        resume_id, name = parse_claude_cmdline_identity(cmdline)
        found.append((pid, cwd, resume_id, name))
    return found


def adopt_untracked_sessions(sessions: list[Session]) -> None:
    """Backfill PID tracking for live `claude` processes Session Hub didn't launch.

    Lets /clear-detection (resolve_clear_continuations) start working on a
    session going forward, even though Session Hub missed its actual launch -
    which every tmux-launched session group member needs, since tmux launches
    never get PID-tracked at launch time (see tmux_group_launch_command).

    Prefers an exact identity read straight off the process's own argv
    (--resume SESSION_ID, or --name NAME matched against that cwd's
    sessions) over guessing. A session group's members all share one cwd, so
    the old "most-recently-updated session in this cwd" guess - the only
    signal available for a plain `claude` typed into a terminal with no
    identifying flags - picked the SAME session for every untracked PID in
    that cwd, corrupting tracking for every member but one: their "Running"
    status came from a sibling's PID instead of their own (staying "Running"
    after their own process died as long as any sibling lived), and their
    own /clear was never detected since resolve_clear_continuations was
    watching the wrong session id entirely. Falls back to the guess only
    when a process has neither flag (the original plain-`claude` case).
    """
    candidates = find_untracked_claude_pids()
    if not candidates:
        return
    by_cwd: dict[str, list[Session]] = {}
    latest_by_cwd: dict[str, Session] = {}
    for session in sessions:
        if session.provider != "Claude":
            continue
        by_cwd.setdefault(session.cwd, []).append(session)
        current = latest_by_cwd.get(session.cwd)
        if not current or session.updated_ms > current.updated_ms:
            latest_by_cwd[session.cwd] = session
    for pid, cwd, resume_id, name in candidates:
        if resume_id:
            record_hub_launch(pid, cwd, resume_id)
            continue
        if name:
            match = next(
                (s for s in by_cwd.get(cwd, []) if s.agent_name == name), None
            )
            if match:
                record_hub_launch(pid, cwd, match.session_id)
            continue
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
    group_session_keys = set()
    for group in metadata.get("groups", {}).values():
        for row in group.get("rows", []):
            session_key = row.get("session_key") or ""
            if session_key.startswith("Claude:"):
                claimed.add(session_key[len("Claude:"):])
                group_session_keys.add(session_key)

    session_overrides = metadata.get("sessions", {})

    # task-2127: a tracked PID whose own session_id is STILL live (no
    # /clear at all - just a leftover old transcript some OTHER cwd
    # sibling's search might otherwise steal a newer session away from) has
    # a strictly weaker claim on any unclaimed newer sibling than a PID
    # whose session_id has genuinely disappeared - the vanished one has no
    # alternative at all, the still-live one always has itself. Resolving
    # genuinely-vanished PIDs first means a still-live PID's own candidate
    # search only ever sees whatever's left over, instead of racing an
    # equally-eligible vanished PID for the same target purely by
    # PID_DIR.glob() iteration order (glob order is filesystem-dependent,
    # not sorted) - that race is what let an unnamed, ungrouped sibling
    # whose own session was still perfectly present get relinked into an
    # unrelated newer same-cwd session that a genuinely /clear'd sibling
    # needed. A lone still-live, unnamed PID (no vanished competitor at
    # all) is unaffected and keeps jumping to a newer sibling exactly as
    # before - see test_resolve_clear_continuations_copies_organic_title_with_no_explicit_override.
    def _has_self_match(item: tuple[Path, dict]) -> bool:
        _, entry = item
        cwd_sessions = by_cwd.get(entry.get("cwd"), [])
        return any(session.session_id == entry.get("session_id") for session in cwd_sessions)

    tracked.sort(key=_has_self_match)

    changed = False
    for tracking_file, entry in tracked:
        old_session_id = entry.get("session_id")
        cwd_sessions = by_cwd.get(entry.get("cwd"), [])
        old_key = f"Claude:{old_session_id}" if old_session_id else None
        # An already-identified old session - explicitly named, or the
        # current session_key of a saved group row - must never be treated
        # as "gone" just because some unrelated, more recently updated
        # sibling shares its cwd. Mirror of the already-named-sibling guard
        # below (which stops an unrelated session being absorbed AS a
        # target): same VAMPULSE-orchestrator incident from the other
        # direction, where the identified session was discarded AS the
        # source - orchestrator's session_id was still perfectly valid, it
        # just lost the "most recently updated in this shared cwd" race to
        # Vampulse-sonnet1's own, unrelated activity, and got /clear-linked
        # into sonnet1's session instead of being left alone.
        if (
            old_key
            and any(session.session_id == old_session_id for session in cwd_sessions)
            and (session_overrides.get(old_key, {}).get("name") or old_key in group_session_keys)
        ):
            continue
        candidates = [
            session
            for session in cwd_sessions
            if session.session_id == old_session_id
            or (
                session.session_id not in claimed
                # A session the user has already explicitly named is a
                # deliberately distinct, identified session - not a fresh,
                # anonymous /clear continuation to silently absorb. Group
                # directories intentionally hold many sessions sharing one
                # cwd, so without this, "most recently updated session in
                # this cwd" can and does pick a totally unrelated, already-
                # named sibling (see the VAMPULSE-orchestrator/VAMPULSE-old
                # incident this guards against).
                and not session_overrides.get(session.native_key, {}).get("name")
            )
        ]
        latest = max(candidates, key=lambda session: session.updated_ms, default=None)
        if not latest:
            continue
        if old_session_id == latest.session_id:
            continue
        if old_session_id is None:
            pending_model = entry.pop("pending_model", None)
            entry["session_id"] = latest.session_id
            tracking_file.write_text(json.dumps(entry))
            claimed.add(latest.session_id)
            if pending_model:
                overrides = metadata.setdefault("sessions", {}).setdefault(
                    latest.native_key, {}
                )
                overrides.setdefault("env", {})["ANTHROPIC_MODEL"] = pending_model
                changed = True
            continue

        new_key = latest.native_key
        old_session = next(
            (s for s in cwd_sessions if s.session_id == old_session_id),
            None,
        )
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


# The canonical VAMPULSE-game checkout. `~/.codex/config.toml`'s
# `[mcp_servers.vampulse]` block is a flat, unconditional GLOBAL entry with
# no per-project scoping of its own, so every Codex session loads it
# regardless of cwd - confirmed live: a session launched into plain
# ~/projects logged `MCP client for 'vampulse' failed to start`. Session Hub
# is the only thing that knows which cwd a launch is headed for, so scoping
# has to happen here, as a launch-time argv override, never by editing that
# global file (which would also affect every non-Session-Hub codex
# invocation and the user's other configured MCPs).
VAMPULSE_PROJECT_ROOT = Path("/home/user/projects/vampulse/VAMPULSE-game")


def _resolve_real_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def vampulse_governed_worktrees(root: Path = VAMPULSE_PROJECT_ROOT) -> list[Path]:
    """Every real `git worktree` of the canonical VAMPULSE-game checkout,
    resolved. Queried live (bounded, single subprocess, only while building a
    Codex launch command) rather than hardcoded - the worktree set changes as
    lanes are opened/closed and a stale hardcoded list would silently exclude
    a currently-governed worktree or include a torn-down one.
    """
    resolved_root = _resolve_real_path(root)
    if resolved_root is None or not resolved_root.is_dir():
        return []
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=resolved_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # ValueError/TypeError: a caller that broadly mocks subprocess.Popen
        # to keep an unrelated test hermetic (this file does that in several
        # places) breaks subprocess.run's own internals the same way a real
        # git failure would look from here - either way, an unusable worktree
        # lookup must fail CLOSED (no worktrees recognized as governed), not
        # crash the launch it's a side-check for.
        return []
    worktrees = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            resolved = _resolve_real_path(Path(line[len("worktree "):]))
            if resolved is not None:
                worktrees.append(resolved)
    return worktrees


def vampulse_mcp_applies(cwd: str | Path) -> bool:
    """True only when `cwd` is the canonical VAMPULSE-game checkout, inside
    it, or inside one of its real git worktrees.

    Both sides are resolved to their real filesystem path first (symlinks
    followed, `..` collapsed) and compared with Path.is_relative_to rather
    than a string prefix - a plain `str(cwd).startswith(str(root))` would
    wrongly admit a textual-prefix impostor like
    "/home/.../VAMPULSE-game-old" (which starts with the root's own string
    but is a different directory), and would wrongly admit or reject a
    symlink depending on which side it sits on. Fails closed (False) if the
    canonical root doesn't exist or `cwd` can't be resolved - an
    unrecognized or broken path never gets the MCP.
    """
    resolved_cwd = _resolve_real_path(Path(cwd))
    if resolved_cwd is None:
        return False
    # VAMPULSE_PROJECT_ROOT passed explicitly, not left to
    # vampulse_governed_worktrees's own default parameter: a default
    # argument value binds to the ORIGINAL module-level object at function
    # definition time, so a test (or any future caller) patching
    # session_hub.VAMPULSE_PROJECT_ROOT would silently be ignored here
    # otherwise - the function would keep resolving worktrees of the real
    # canonical root no matter what was patched.
    roots = [
        root
        for root in (
            [_resolve_real_path(VAMPULSE_PROJECT_ROOT)]
            + vampulse_governed_worktrees(VAMPULSE_PROJECT_ROOT)
        )
        if root is not None
    ]
    return any(resolved_cwd == root or resolved_cwd.is_relative_to(root) for root in roots)


def codex_launch_args(
    cwd: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    danger_mode: bool = False,
    session_id: str | None = None,
    source_cwd: str | None = None,
    initial_prompt: str | None = None,
) -> list[str]:
    """The Codex CLI argv shared by every launch path (direct terminal,
    tmux, resume) - one place that decides model/effort/MCP-scope flags so
    they can't drift between the direct-terminal and tmux code paths the way
    two independently-maintained copies of this list already had (dual-write
    class; see docs/netcode_dual_write_audit.md's game-code equivalent).

    MCP scope is decided from `execution_cwd` (source_cwd-or-cwd for a
    resume, cwd for a new session) - NOT the bare `cwd` parameter (row447
    fourth rework). `codex resume -C <dir>` actually runs in `source_cwd or
    cwd`, so scoping off `cwd` alone diverges from the real process
    directory in both directions: a resume whose DISPLAY cwd is inside
    VAMPULSE but whose real source_cwd is not would leak the MCP into a
    directory it must never reach; a resume whose display cwd is outside
    VAMPULSE but whose real source_cwd is inside it would wrongly disable
    the MCP for a legitimately-scoped session.
    """
    args = [executable("codex")]
    if danger_mode:
        args += ["--dangerously-bypass-approvals-and-sandbox"]
    if model:
        args += ["-m", model]
    if reasoning_effort:
        args += ["-c", f"model_reasoning_effort={reasoning_effort}"]
    execution_cwd = (source_cwd or cwd) if session_id else cwd
    if not vampulse_mcp_applies(execution_cwd):
        args += ["-c", "mcp_servers.vampulse.enabled=false"]
    if session_id:
        # source_cwd, not cwd: `codex resume -C <dir>` silently FORKS into a
        # brand-new near-empty thread instead of erroring when <dir> differs
        # from the session's own root - observed directly (a cwd-drifted
        # group row, e.g. one that cd'd into a worktree, forked twice and
        # died with no visible output under tmux). Resuming in the session's
        # actual directory avoids triggering that fork at all.
        args += ["resume", "-C", execution_cwd, session_id]
    else:
        args += ["-C", execution_cwd]
    if initial_prompt:
        args += [initial_prompt]
    return args


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
        self.launch_in_tmux = QCheckBox("Launch every session in tmux by default")
        self.launch_in_tmux.setToolTip(
            "Resumes and new group-row launches run detached inside tmux "
            "unless a session's own \"Launch in tmux\" checkbox (right-click "
            "a session → Launch options…, or a group's own checkbox) has "
            "been explicitly set to override this."
        )
        self.launch_in_tmux.setChecked(bool(settings.get("launch_in_tmux", False)))
        launch_layout.addWidget(self.launch_in_tmux)
        self.launch_options = LaunchOptionsEditor(
            settings.get("global_env") or {}, settings.get("global_flags") or {}
        )
        self.env_editor = self.launch_options.env_editor
        self.flags_editor = self.launch_options.flags_editor
        launch_layout.addWidget(self.launch_options)
        layout.addWidget(launch_group)

        self.status_hooks_enabled = QCheckBox(
            "Enable live session status (Working / Needs input / Done)"
        )
        self.status_hooks_enabled.setToolTip(
            "Shows a live status column on the Running tab. For Claude, merges "
            "hooks into each launched project's own .claude/settings.local.json "
            "(gitignored, not committed). For Codex, sets the `notify` command "
            "in ~/.codex/config.toml - unlike Claude this is a single, machine-"
            "wide setting, not per-project, so it also affects Codex sessions "
            "run outside Session Hub, and only gives a Done signal (no Working/"
            "Needs input - Codex doesn't expose those). Turning this off removes "
            "exactly what Session Hub added, everywhere it added it. Off by "
            "default since it edits files outside this app."
        )
        self.status_hooks_enabled.setChecked(bool(settings.get("status_hooks_enabled", False)))
        layout.addWidget(self.status_hooks_enabled)

        self.enable_accounts = QCheckBox("Enable multiple Claude accounts")
        self.enable_accounts.setToolTip(
            "Adds an Account picker (alongside Model) to New Session, group "
            "rows, and agent handoffs, for switching which logged-in Claude "
            "Code account (CLAUDE_CONFIG_DIR) a session launches as. Off by "
            "default until a second account actually exists - see "
            "setup_claude_account.sh."
        )
        self.enable_accounts.setChecked(bool(settings.get("claude_accounts_enabled", False)))
        layout.addWidget(self.enable_accounts)

        accounts_group = QGroupBox("Claude accounts")
        accounts_layout = QVBoxLayout(accounts_group)
        accounts_note = QLabel(
            "Name each Claude Code account you're logged into and its "
            "CLAUDE_CONFIG_DIR (e.g. ~/.claude-2, provisioned with "
            "setup_claude_account.sh). Pick one per session/group row from "
            "its launch options or the New Session dialog."
        )
        accounts_note.setWordWrap(True)
        accounts_layout.addWidget(accounts_note)
        self.accounts_editor = EnvEditor(
            settings.get("claude_accounts") or DEFAULT_CLAUDE_ACCOUNTS,
            specs={},
            name_label="Account name",
            item_noun="account",
            custom_description="CLAUDE_CONFIG_DIR for this account.",
        )
        accounts_layout.addWidget(self.accounts_editor)
        accounts_group.setVisible(self.enable_accounts.isChecked())
        self.enable_accounts.toggled.connect(accounts_group.setVisible)
        layout.addWidget(accounts_group)

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
                "claude_accounts_enabled": self.enable_accounts.isChecked(),
                "claude_accounts": self.accounts_editor.env(),
                "launch_in_tmux": self.launch_in_tmux.isChecked(),
                "status_hooks_enabled": self.status_hooks_enabled.isChecked(),
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
        self.reasoning_effort: str | None = None
        self.account_config_dir: str | None = None
        self.use_tmux: bool = bool(settings.get("launch_in_tmux", False))
        self.claude_accounts = settings.get("claude_accounts") or DEFAULT_CLAUDE_ACCOUNTS
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
        self.account_combo: QComboBox | None = None
        self.codex_model_combo: QComboBox | None = None
        self.codex_effort_combo: QComboBox | None = None
        if provider == "Claude":
            self.model_combo = QComboBox()
            for label, alias in CLAUDE_MODELS:
                self.model_combo.addItem(label, alias)
            form.addRow("Model:", self.model_combo)
            if settings.get("claude_accounts_enabled"):
                self.account_combo = QComboBox()
                populate_claude_account_combo(self.account_combo, self.claude_accounts, None)
                form.addRow("Account:", self.account_combo)
        elif provider == "Codex":
            self.codex_model_combo = QComboBox()
            populate_codex_model_combo(self.codex_model_combo, None)
            form.addRow("Model:", self.codex_model_combo)
            self.codex_effort_combo = QComboBox()
            populate_codex_effort_combo(self.codex_effort_combo, None, None)
            form.addRow("Effort:", self.codex_effort_combo)
            self.codex_model_combo.currentIndexChanged.connect(
                lambda: populate_codex_effort_combo(
                    self.codex_effort_combo, codex_combo_value(self.codex_model_combo), None
                )
            )

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

        self.tmux_checkbox = QCheckBox("Launch detached inside tmux")
        self.tmux_checkbox.setChecked(self.use_tmux)
        self.tmux_checkbox.setToolTip(
            "Runs this session inside its own tmux session instead of "
            "directly in a terminal, so tooling can drive it via "
            "`tmux send-keys` with no focus impact. Requires tmux to be "
            "installed. Defaults to the global Settings toggle, but can be "
            "changed for this session only."
        )
        layout.addWidget(self.tmux_checkbox)

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
            if self.account_combo is not None:
                self.account_config_dir = self.account_combo.currentData()
        elif self.codex_model_combo is not None:
            self.model = codex_combo_value(self.codex_model_combo)
            self.reasoning_effort = codex_combo_value(self.codex_effort_combo)
            reason = invalid_codex_model_effort_reason(self.model, self.reasoning_effort)
            if reason:
                QMessageBox.warning(self, "Unsupported model/effort", reason)
                return
        self.use_tmux = self.tmux_checkbox.isChecked()
        super().accept()


TRANSCRIPT_READ_PROMPT = (
    "Read the transcript at the path above. If it contains a /compact "
    "summary, read from there onward; otherwise read roughly the last 150 "
    "messages. Inspect the current project state, then continue the task "
    "naturally."
)


class TranscriptPathDialog(QDialog):
    """Shows `session`'s on-disk transcript path so the user can copy it (and
    optionally a suggested read prompt) and paste it into the new `target`
    session themselves, after setting model/effort/account there - replaces
    the old auto-written handoff .md + auto-sent initial prompt (user
    2026-08-27: wants to paste it manually, not have Session Hub send a
    message before they've had a chance to configure the new session)."""

    def __init__(self, session: Session, target: str, parent=None) -> None:
        super().__init__(parent)
        self.include_prompt = False
        self.setWindowTitle(f"Continue with {target}")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Transcript for “{session.title}” ({session.provider}) is on disk. Copy "
            f"the path below - or the path plus a suggested read prompt - to paste "
            f"into the new {target} session yourself."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        path_edit = QLineEdit(str(session.path))
        path_edit.setReadOnly(True)
        layout.addWidget(path_edit)

        buttons = QHBoxLayout()
        copy_path_btn = QPushButton("Copy path")
        copy_path_btn.clicked.connect(lambda: self._accept(False))
        buttons.addWidget(copy_path_btn)
        copy_prompt_btn = QPushButton("Copy path + prompt")
        copy_prompt_btn.clicked.connect(lambda: self._accept(True))
        buttons.addWidget(copy_prompt_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _accept(self, include_prompt: bool) -> None:
        self.include_prompt = include_prompt
        self.accept()


class AgentModelEffortDialog(QDialog):
    """Model/effort picker for a single already-known provider.

    Used by continue_with_other_agent_for the first time it hands a session
    to a provider with no existing linked session to swap back into instead
    (see SessionHub.continue_with_other_agent_for) - same combo widgets
    NewSessionDialog builds for its own provider-conditioned fields, minus
    the provider choice itself, since that's already been made by the time
    this dialog opens. default_model/default_reasoning_effort preselect the
    group's (or session's) already-configured launch options for `provider`,
    so a first-time handoff doesn't silently fall back to "Default" even
    though this group has always launched that provider with e.g. Opus.
    """

    def __init__(
        self,
        provider: str,
        parent=None,
        default_model: str | None = None,
        default_reasoning_effort: str | None = None,
        claude_accounts: dict[str, str] | None = None,
        default_account: str | None = None,
        accounts_enabled: bool = False,
    ) -> None:
        super().__init__(parent)
        self.provider = provider
        self.model: str | None = None
        self.reasoning_effort: str | None = None
        self.account_config_dir: str | None = None
        self.setWindowTitle(f"{provider} model")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.model_combo: QComboBox | None = None
        self.account_combo: QComboBox | None = None
        self.codex_model_combo: QComboBox | None = None
        self.codex_effort_combo: QComboBox | None = None
        if provider == "Claude":
            self.model_combo = QComboBox()
            for label, alias in CLAUDE_MODELS:
                self.model_combo.addItem(label, alias)
            index = self.model_combo.findData(default_model) if default_model else -1
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
            form.addRow("Model:", self.model_combo)
            if accounts_enabled:
                self.account_combo = QComboBox()
                populate_claude_account_combo(
                    self.account_combo, claude_accounts or DEFAULT_CLAUDE_ACCOUNTS, default_account
                )
                form.addRow("Account:", self.account_combo)
        elif provider == "Codex":
            self.codex_model_combo = QComboBox()
            populate_codex_model_combo(self.codex_model_combo, default_model)
            form.addRow("Model:", self.codex_model_combo)
            self.codex_effort_combo = QComboBox()
            populate_codex_effort_combo(self.codex_effort_combo, default_model, default_reasoning_effort)
            form.addRow("Effort:", self.codex_effort_combo)
            self.codex_model_combo.currentIndexChanged.connect(
                lambda: populate_codex_effort_combo(
                    self.codex_effort_combo, codex_combo_value(self.codex_model_combo), None
                )
            )
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if self.model_combo is not None:
            self.model = self.model_combo.currentData()
            if self.account_combo is not None:
                self.account_config_dir = self.account_combo.currentData()
        elif self.codex_model_combo is not None:
            self.model = codex_combo_value(self.codex_model_combo)
            self.reasoning_effort = codex_combo_value(self.codex_effort_combo)
            reason = invalid_codex_model_effort_reason(self.model, self.reasoning_effort)
            if reason:
                QMessageBox.warning(self, "Unsupported model/effort", reason)
                return
        super().accept()


class LaunchNewGroupSessionsDialog(QDialog):
    """Define new named sessions to launch into an already-known group directory.

    Row-table UI only - no directory picker, since the caller (ManageGroupDialog)
    already knows the group's cwd. Each row independently picks a provider
    (Claude or Codex), a model, and a session name, auto-suggested but always
    editable. Model is a dropdown for both providers - CLAUDE_MODELS' fixed
    aliases for Claude, codex_models()'s live-fetched roster for Codex (still
    editable, since that cache can be stale or incomplete). Codex rows also
    get an Effort dropdown, populated from the selected model's own supported
    reasoning levels; Claude rows have no per-row equivalent here (its effort
    is the existing global "--effort" CLI flag, edited via Launch options).
    """

    def __init__(
        self,
        cwd: str,
        existing_names: set[str],
        tmux: bool,
        parent=None,
        claude_accounts: dict[str, str] | None = None,
        accounts_enabled: bool = False,
        will_launch: bool = True,
    ) -> None:
        super().__init__(parent)
        self.cwd = cwd
        self.existing_names = existing_names
        self.group_rows: list[dict] = []
        self.use_tmux: bool = tmux
        self.claude_accounts = claude_accounts or DEFAULT_CLAUDE_ACCOUNTS
        self.accounts_enabled = accounts_enabled
        self.will_launch = will_launch
        self.setWindowTitle("Launch new sessions" if will_launch else "Add new sessions")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        directory_label = QLabel(f"Working directory: {cwd}")
        directory_label.setWordWrap(True)
        layout.addWidget(directory_label)

        # will_launch=False ("Add new…"): the tmux choice has no effect here
        # (register_group_row/add_new_rows_into_group never launches
        # anything), so showing it would promise a behavior this dialog
        # doesn't perform - it applies the next time the saved row is
        # actually launched, via the group's own "Launch in tmux" checkbox.
        self.tmux_checkbox = QCheckBox("Launch detached inside tmux")
        self.tmux_checkbox.setChecked(tmux)
        self.tmux_checkbox.setToolTip(
            "Runs each session inside its own tmux session (named to match "
            "--name) instead of directly in a terminal, so tooling can drive "
            "it via `tmux send-keys` with no focus impact. Requires tmux to "
            "be installed. /clear-detection isn't available yet for "
            "tmux-launched sessions."
        )
        self.tmux_checkbox.setVisible(will_launch)
        layout.addWidget(self.tmux_checkbox)

        layout.addWidget(QLabel("Sessions to launch:" if will_launch else "Sessions to add:"))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Provider", "Model", "Effort", "Account", "Name"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnHidden(3, not self.accounts_enabled)
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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Launch" if will_launch else "Add"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_row()

    # -- rows ----------------------------------------------------------
    def add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        provider_combo = QComboBox()
        provider_combo.addItem("Claude", "Claude")
        provider_combo.addItem("Codex", "Codex")
        provider_combo.currentIndexChanged.connect(lambda _i, r=row: self.on_provider_changed(r))
        self.table.setCellWidget(row, 0, provider_combo)
        name_edit = QLineEdit()
        name_edit.auto_suggested = True
        name_edit.textEdited.connect(
            lambda _text, edit=name_edit: setattr(edit, "auto_suggested", False)
        )
        self.table.setCellWidget(row, 4, name_edit)
        self.set_model_widget(row, "Claude")
        self.suggest_name(row)

    def set_model_widget(self, row: int, provider: str) -> None:
        if provider == "Claude":
            model_widget = QComboBox()
            for label, alias in CLAUDE_MODELS:
                model_widget.addItem(label, alias)
            model_widget.setCurrentIndex(1)
            model_widget.currentIndexChanged.connect(lambda _i, r=row: self.suggest_name(r))
            # Claude's effort is the existing global "--effort" CLI flag
            # (edited via Launch options), not a per-row concept here.
            effort_widget: QComboBox | QLabel = QLabel("—")
            if self.accounts_enabled:
                account_widget: QComboBox | QLabel = QComboBox()
                populate_claude_account_combo(account_widget, self.claude_accounts, None)
            else:
                account_widget = QLabel("—")
        else:
            model_widget = QComboBox()
            populate_codex_model_combo(model_widget, None)
            effort_widget = QComboBox()
            populate_codex_effort_combo(effort_widget, None, None)
            model_widget.currentIndexChanged.connect(
                lambda _i, r=row: self.on_codex_model_changed(r)
            )
            # Account is a Claude-only concept - Codex/Antigravity have no
            # equivalent CLAUDE_CONFIG_DIR-style multi-login mechanism here.
            account_widget = QLabel("—")
        self.table.setCellWidget(row, 1, model_widget)
        self.table.setCellWidget(row, 2, effort_widget)
        self.table.setCellWidget(row, 3, account_widget)

    def on_codex_model_changed(self, row: int) -> None:
        model_widget = self.table.cellWidget(row, 1)
        effort_widget = self.table.cellWidget(row, 2)
        if isinstance(model_widget, QComboBox) and isinstance(effort_widget, QComboBox):
            populate_codex_effort_combo(effort_widget, codex_combo_value(model_widget), None)
        self.suggest_name(row)

    def on_provider_changed(self, row: int) -> None:
        provider_combo = self.table.cellWidget(row, 0)
        provider = provider_combo.currentData() if provider_combo else "Claude"
        self.set_model_widget(row, provider)
        self.suggest_name(row)

    def remove_selected_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self.table.removeRow(row)

    def existing_row_names(self, exclude_row: int | None = None) -> set[str]:
        names = set(self.existing_names)
        for row in range(self.table.rowCount()):
            if row == exclude_row:
                continue
            edit = self.table.cellWidget(row, 4)
            if edit is not None and edit.text().strip():
                names.add(edit.text().strip())
        return names

    def suggest_name(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        name_edit = self.table.cellWidget(row, 4)
        if name_edit is None or not getattr(name_edit, "auto_suggested", True):
            return
        model_widget = self.table.cellWidget(row, 1)
        alias = model_widget.currentData() if isinstance(model_widget, QComboBox) else None
        suggested = suggest_session_name(
            Path(self.cwd), alias, self.existing_row_names(exclude_row=row)
        )
        name_edit.blockSignals(True)
        name_edit.setText(suggested)
        name_edit.blockSignals(False)
        name_edit.auto_suggested = True

    def rows(self) -> list[dict]:
        result = []
        for row in range(self.table.rowCount()):
            provider_combo = self.table.cellWidget(row, 0)
            model_widget = self.table.cellWidget(row, 1)
            effort_widget = self.table.cellWidget(row, 2)
            account_widget = self.table.cellWidget(row, 3)
            name_edit = self.table.cellWidget(row, 4)
            raw_name = name_edit.text().strip() if name_edit else ""
            if not raw_name:
                continue
            # Canonicalized here, before accept()'s own duplicate-name check
            # below - two rows typed as "a.b" and "a:b" must collide there
            # (row447 rework), not silently mint two rows sharing one real
            # tmux identity.
            name = sanitize_tmux_session_name(raw_name)
            provider = provider_combo.currentData() if provider_combo else "Claude"
            if provider == "Codex":
                model = codex_combo_value(model_widget) if model_widget else None
                effort = (
                    codex_combo_value(effort_widget)
                    if isinstance(effort_widget, QComboBox)
                    else None
                )
                account_config_dir = None
            else:
                model = model_widget.currentData() if model_widget else None
                effort = None
                account_config_dir = (
                    account_widget.currentData()
                    if isinstance(account_widget, QComboBox)
                    else None
                )
            result.append(
                {
                    "name": name,
                    "provider": provider,
                    "model": model,
                    "reasoning_effort": effort,
                    "account_config_dir": account_config_dir,
                }
            )
        return result

    def accept(self) -> None:
        rows = self.rows()
        if not rows:
            QMessageBox.warning(
                self, "No sessions", "Add at least one row with a name."
            )
            return
        seen: set[str] = set()
        for row in rows:
            if row["name"] in seen or row["name"] in self.existing_names:
                QMessageBox.warning(
                    self,
                    "Duplicate name",
                    f"The name “{row['name']}” is used more than once.",
                )
                return
            seen.add(row["name"])
            if row["provider"] == "Codex":
                reason = invalid_codex_model_effort_reason(
                    row["model"], row["reasoning_effort"]
                )
                if reason:
                    QMessageBox.warning(self, "Unsupported model/effort", reason)
                    return

        self.group_rows = rows
        self.use_tmux = self.tmux_checkbox.isChecked()
        super().accept()


class MoveToGroupDialog(QDialog):
    """Pick an existing saved group to move an already-running session into.

    No Model/Name fields and nothing gets launched - this only files the
    already-selected session into `group["rows"]` (see
    SessionHub.add_session_to_group_for). `initial_cwd`, when it names a
    real group, just preselects that row as a convenience.
    """

    NEW_GROUP = "__new__"
    new_group_name: str | None = None

    def __init__(self, groups: dict, initial_cwd: str | None, parent=None) -> None:
        super().__init__(parent)
        self.groups = groups
        self.cwd: str | None = None
        self.new_group_name: str | None = None
        self.setWindowTitle("Add session to group")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.group_combo = QComboBox()
        self.group_combo.addItem("New group…", self.NEW_GROUP)
        for cwd, group in sorted(
            groups.items(),
            key=lambda item: item[1].get("display_name") or Path(item[0]).name or item[0],
        ):
            label = group.get("display_name") or Path(cwd).name or cwd
            self.group_combo.addItem(label, cwd)
        if initial_cwd is not None:
            index = self.group_combo.findData(initial_cwd)
            if index >= 0:
                self.group_combo.setCurrentIndex(index)
        self.group_combo.currentIndexChanged.connect(self.on_group_changed)
        form.addRow("Group:", self.group_combo)

        self.directory_label = QLabel()
        self.directory_label.setWordWrap(True)
        form.addRow("Directory:", self.directory_label)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.on_group_changed()

    def on_group_changed(self) -> None:
        data = self.group_combo.currentData()
        if data == self.NEW_GROUP:
            self.directory_label.setText("The session's own working directory")
        else:
            self.directory_label.setText(data or "")

    def accept(self) -> None:
        data = self.group_combo.currentData()
        if data == self.NEW_GROUP:
            name, accepted = QInputDialog.getText(self, "New group", "Group name:")
            name = name.strip()
            if not accepted or not name:
                return
            self.new_group_name = name
            self.cwd = None
        else:
            self.cwd = data
        super().accept()


class PickGroupSessionDialog(QDialog):
    """Pick an already-running session to file into this group - no launch.

    `sessions` is every ungrouped session the hub knows about (any cwd,
    any provider) - the target group's cwd is already fixed by the
    ManageGroupDialog this is opened from, so there's nothing to pick but
    which session. See SessionHub.file_session_into_group.
    """

    def __init__(self, sessions: list["Session"], parent=None) -> None:
        super().__init__(parent)
        self.sessions = sessions
        self.session: "Session | None" = None
        self.setWindowTitle("Add session to group")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.session_combo = QComboBox()
        for session in sessions:
            self.session_combo.addItem(f"{session.title} — {session.cwd}")
        form.addRow("Session:", self.session_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Add")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        index = self.session_combo.currentIndex()
        if 0 <= index < len(self.sessions):
            self.session = self.sessions[index]
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
        # This dialog keeps its own launch-liveness STATUS_COLUMN
        # (Running/Stopped, via group_row_status), separate from Session ID.
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
            # act on), Agent moved to just before Session ID since most
            # groups are single-provider in practice. Only applied when
            # there's no saved order yet - once the user drags one, that wins.
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
        # ExtendedSelection: plain click selects one row, Ctrl+click toggles
        # extra rows into the selection, Shift+click selects a range - the
        # usual Qt multi-select gesture. launch_selected_rows() (wired below
        # to Enter/Return via _GroupSessionTable.keyPressEvent) acts on
        # however many rows that leaves selected.
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # Double-click is wired to launch/resume below; without this, Qt's
        # default edit triggers also open the cell for inline text editing
        # on the same double-click, which looks like (and was mistaken for)
        # a rename.
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.row_context_menu)
        self.table.doubleClicked.connect(self.launch_or_resume_row)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.launch_selected_rows)
        self.table.itemSelectionChanged.connect(self._update_launch_selected_enabled)
        layout.addWidget(self.table)

        controls = QHBoxLayout()
        self.launch_selected_button = QPushButton("Launch selected")
        self.launch_selected_button.setToolTip(
            "Launch or resume every currently-selected row - nothing "
            "happens with no selection."
        )
        self.launch_selected_button.setEnabled(False)
        self.launch_selected_button.clicked.connect(self.launch_selected_rows)
        controls.addWidget(self.launch_selected_button)
        add_session_button = QPushButton("Add session…")
        add_session_button.setToolTip(
            "File an already-running session into this group - no new "
            "session gets launched."
        )
        add_session_button.clicked.connect(self.add_existing_session)
        controls.addWidget(add_session_button)
        add_new_button = QPushButton("Add new…")
        add_new_button.setToolTip(
            "Define one or more brand-new named sessions (Claude or Codex) "
            "and save them into this group - nothing gets launched."
        )
        add_new_button.clicked.connect(self.add_new_rows)
        controls.addWidget(add_new_button)
        group_options_button = QPushButton("Group launch options…")
        group_options_button.setToolTip(
            "Env vars and CLI flags applied to every row in this group. "
            "Override the global settings; a row's own launch options "
            "(right-click a row → Launch options…) override these."
        )
        group_options_button.clicked.connect(self.edit_group_launch_options)
        controls.addWidget(group_options_button)
        self.tmux_checkbox = QCheckBox("Launch in tmux")
        self.tmux_checkbox.setToolTip(
            "New launches of this group's members run detached inside tmux "
            "instead of directly in a terminal. Already-running sessions "
            "are unaffected until relaunched."
        )
        group = self.group()
        self.tmux_checkbox.setChecked(
            self.hub.effective_tmux(group.get("tmux") if group else None)
        )
        self.tmux_checkbox.toggled.connect(self.set_tmux)
        controls.addWidget(self.tmux_checkbox)
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
        """Upgrade rows saved before override_key/live-status/provider existed.

        Drops the old sticky `launched` flag (status is now always derived
        live), mints an `override_key` for any row that predates it (carrying
        its old `model` choice into the same override mechanism a fresh row
        uses - see SessionHub.register_group_row), and backfills `provider`
        as `"Claude"` for any row saved before groups could hold Codex rows
        too.
        """
        changed = False
        for row in group.get("rows", []):
            if row.pop("launched", None) is not None:
                changed = True
            if "provider" not in row:
                row["provider"] = "Claude"
                changed = True
            if "override_key" not in row:
                registered = self.hub.register_group_row(
                    self.cwd, row["name"], row["provider"], row.pop("model", None)
                )
                row["override_key"] = registered["override_key"]
                changed = True
        if changed:
            write_metadata(self.hub.metadata)

    def matched_sessions(self) -> list[tuple[dict, Session | None]]:
        """Each saved row paired with its live session, if currently matched.

        Applies the same per-session title/cwd overrides discover_sessions
        applies - group members are hidden from self.hub.sessions (that's
        the point of the group-collapsing pass), so this re-derives from the
        raw per-provider session lists rather than reusing that already-
        filtered list.

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
        overrides = self.hub.metadata.get("sessions", {})
        # group_row_candidates() is the shared resolver discover_sessions,
        # launch_group_row and resume_group_row also call - it applies
        # metadata["links"] to the raw per-provider lists (they never do
        # this on their own), which is how a row whose provider was
        # overtaken by a linked continuation in another provider still
        # matches here.
        live = group_row_candidates(self.hub.metadata, self.hub.settings())
        pairs = []
        claimed: set[str] = set()
        for row in group.get("rows", []):
            match = find_group_member_session(row, self.cwd, live, frozenset(claimed))
            if match:
                claimed.add(match.native_key)
                row_custom = overrides.get(row["override_key"], {})
                native_custom = overrides.get(match.key, {})
                match.title = (
                    row_custom.get("name") or native_custom.get("name") or match.title
                )
                match.cwd = row_custom.get("cwd") or native_custom.get("cwd") or match.cwd
            pairs.append((row, match))
        return pairs

    def row_session(self, row: dict, match: Session | None) -> Session:
        base = match or Session(
            row.get("provider", "Claude"),
            f"pending:{row['override_key']}",
            row["name"], self.cwd, self.cwd, 0,
            Path(self.cwd),
        )
        # title=row["name"], not just logical_key: rename_group_row_in makes
        # row["name"] the row's one authoritative label and deliberately
        # drops any separate override["name"] once it moves the bucket - a
        # matched (already-running) row's own base.title is still whatever
        # the live process was launched with, so without this a rename kept
        # writing the new name to metadata/tmux correctly but the table went
        # on showing the pre-rename name forever.
        return replace(base, logical_key=row["override_key"], title=row["name"])

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

    def reload(self, select_override_keys: set[str] | None = None) -> None:
        """Repopulate the table, optionally reselecting rows by override_key.

        populate_session_table() replaces every QTableWidgetItem, which
        drops Qt's selection outright - without `select_override_keys`, a
        launch/resume click that changes a row's "Last updated" (and so its
        sorted position) left the highlight sitting on whatever row happened
        to land in that visual slot next, not the row the user actually
        launched.
        """
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
        # One tmux snapshot for every row in this dialog, not one subprocess
        # per row (see tmux_live_session_names).
        live_names = tmux_live_session_names()
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

            status = group_row_status(
                row, match, self.hub.effective_tmux(group.get("tmux")), live_names
            )
            self.table.setItem(index, self.STATUS_COLUMN, QTableWidgetItem(status))
        if select_override_keys:
            for row_index in range(self.table.rowCount()):
                item = self.table.item(row_index, 0)
                if item and item.data(Qt.ItemDataRole.UserRole + 1) in select_override_keys:
                    self.table.selectRow(row_index)

    def set_transcripts(self, name: str, enabled: bool) -> None:
        group = self.group()
        if not group:
            return
        row = next((r for r in group["rows"] if r["name"] == name), None)
        if not row:
            return
        row["transcripts"] = enabled
        write_metadata(self.hub.metadata)

    def set_tmux(self, enabled: bool) -> None:
        group = self.group()
        if not group:
            return
        group["tmux"] = enabled
        write_metadata(self.hub.metadata)

    def launch_row(self, name: str) -> None:
        group = self.group()
        override_key = next(
            (r["override_key"] for r in (group.get("rows", []) if group else []) if r["name"] == name),
            None,
        )
        self.hub.launch_group_row(self.cwd, name)
        self.hub.refresh()
        self.reload(select_override_keys={override_key} if override_key else None)

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
            self.hub.resume_group_row(self.cwd, row["name"])
        else:
            self.hub.launch_group_row(self.cwd, row["name"])
        self.hub.refresh()
        self.reload(select_override_keys={row["override_key"]})

    def _update_launch_selected_enabled(self) -> None:
        self.launch_selected_button.setEnabled(
            bool(self.table.selectionModel().selectedRows())
        )

    def launch_selected_rows(self) -> None:
        """"Launch selected" button / Enter/Return on the table: launch or
        resume every selected row, in table order.

        Mirrors launch_or_resume_row's per-row launch-vs-resume choice, but
        for the whole (Ctrl/Shift-click) selection at once instead of just
        the row under the cursor. Table order (not selection/click order)
        keeps multi-row launches deterministic and matches what the user
        sees top to bottom.
        """
        table_rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not table_rows:
            return
        override_keys = set()
        for table_row in table_rows:
            pair = self.pair_at_table_row(table_row)
            if pair is None:
                continue
            row, match = pair
            override_keys.add(row["override_key"])
            if match:
                self.hub.resume_group_row(self.cwd, row["name"])
            else:
                self.hub.launch_group_row(self.cwd, row["name"])
        self.hub.refresh()
        self.reload(select_override_keys=override_keys)

    def edit_group_launch_options(self) -> None:
        self.hub.edit_group_launch_options(self.cwd)
        self.reload()

    def add_existing_session(self) -> None:
        # self.hub.sessions already excludes group members (see
        # matched_sessions), so every entry here is a legitimate,
        # not-yet-grouped session - any cwd, any provider is fine, since
        # file_session_into_group applies a cwd override the same way
        # add_session_to_group_for does.
        eligible = list(self.hub.sessions)
        if not eligible:
            QMessageBox.information(
                self,
                "No sessions available",
                "There are no other sessions to add to this group.",
            )
            return
        dialog = PickGroupSessionDialog(eligible, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.session:
            return
        self.hub.file_session_into_group(dialog.session, self.cwd)
        self.reload()

    def add_new_rows(self) -> None:
        group = self.group()
        existing_names = {row["name"] for row in group.get("rows", [])} if group else set()
        dialog = LaunchNewGroupSessionsDialog(
            self.cwd,
            existing_names,
            self.tmux_checkbox.isChecked(),
            self,
            claude_accounts=self.hub.settings().get("claude_accounts") or DEFAULT_CLAUDE_ACCOUNTS,
            accounts_enabled=bool(self.hub.settings().get("claude_accounts_enabled")),
            will_launch=False,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.group_rows:
            return
        self.hub.add_new_rows_into_group(self.cwd, dialog.group_rows)
        self.reload()

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
        # rowAt(), not itemAt(): the Transcripts column is a checkbox cell
        # widget with no backing QTableWidgetItem, so itemAt() returns None
        # over its entire width and the menu silently failed to open at all
        # for a right-click anywhere on that column.
        row_index = self.table.rowAt(point.y())
        if row_index < 0:
            return
        # pair_at_table_row(), not pairs[row_index]: table row index is a
        # visual position that drifts from matched_sessions()'s own order
        # once the table is sorted, and matched_sessions() (not a fresh
        # find_group_member_session() call) is the one place that resolves
        # title/cwd overrides *and* linked_keys (from metadata["links"])
        # onto the matched session - recomputing the match independently
        # skipped that, which is why "Open linked conversation..." always
        # came back empty for a group row even when it really was linked.
        pair = self.pair_at_table_row(row_index)
        if pair is None:
            return
        row, match = pair
        menu = QMenu(self)
        # row_session(), not the raw match: its .key is the row's own
        # override_key, so "Launch options..." reads/writes the exact
        # bucket the Model column already reads from (effective_model)
        # instead of a native session key that goes stale on every
        # restart and never matches what the column shows. row_session()
        # returns a usable (pending:) Session even with no live match, so
        # this runs for a never-launched row too -- Rename must work on a
        # saved row that hasn't started yet, not just a running one.
        session = self.row_session(row, match)
        for label, slot in self.hub.context_menu_actions(session):
            if label == "Add session to group…":
                continue
            if not match and label != "Rename":
                continue
            if label == "Resume in new terminal":
                # Not the generic bound slot: that calls hub.launch()
                # directly with no use_tmux/tmux_name, so a tmux-enabled
                # group silently resumed in a plain terminal instead.
                # resume_group_row is the same tmux-aware path double-
                # click already uses.
                # 0-arg closure over `row` (fixed for this whole menu, not a
                # loop variable here), NOT a `lambda n=row["name"]: ...`
                # default-arg capture: QAction.triggered always emits a
                # `checked` bool, and PyQt fills a 1-parameter slot's sole
                # parameter with that bool instead of leaving its default -
                # silently replacing the row name with `False` and making
                # the action a no-op.
                slot = lambda: self.hub.resume_group_row(self.cwd, row["name"])
            if label == "Rename":
                # The ROW is renamed, not a display override layered over
                # it -- see rename_group_row_in. One name: table, --name,
                # tmux session, terminal title.
                slot = lambda: self.rename_row(row["name"])
            action = QAction(label, self)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.addSeparator()
        remove_action = QAction("Remove from group", self)
        remove_action.triggered.connect(lambda: self.remove_row(row["name"]))
        menu.addAction(remove_action)
        menu.exec(self.table.viewport().mapToGlobal(point))
        self.reload()

    def rename_row(self, name: str) -> None:
        new, accepted = QInputDialog.getText(self, "Rename row", "Name:", text=name)
        if not accepted or not new.strip():
            return
        result = self.hub.rename_group_row(self.cwd, name, new)
        if result["status"] == "error":
            QMessageBox.warning(self, "Rename row", result["message"])
        self.reload()

    def remove_row(self, name: str) -> None:
        group = self.group()
        if not group:
            return
        row = next((r for r in group["rows"] if r["name"] == name), None)
        if row:
            # A group member's model/name/cwd/flags overrides live under its
            # synthetic override_key (see register_group_row, matched_sessions),
            # not under the live session's own native key. Once the row is
            # removed, nothing looks in that bucket anymore - so without this,
            # the session silently reverts to defaults (e.g. loses "launch as
            # opus") the moment it leaves the group.
            provider = row.get("provider", "Claude")
            candidates = claude_sessions() if provider == "Claude" else codex_sessions()
            match = find_group_member_session(row, self.cwd, candidates)
            if match:
                overrides = self.hub.metadata.setdefault("sessions", {})
                row_overrides = overrides.get(row["override_key"], {})
                native_overrides = overrides.setdefault(match.native_key, {})
                for field, value in row_overrides.items():
                    native_overrides.setdefault(field, value)
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
        # task-2142: compact one-line usage summary (label + tiny bar per
        # enabled provider). The full per-window grid is now the "Expand"
        # detail view, hidden until the user opens it - deliberately not a
        # saved setting, so every launch starts compact.
        self.usage_compact_labels: dict[str, QLabel] = {}
        self.usage_compact_bars: dict[str, QProgressBar] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self.group_dialogs: dict[str, "ManageGroupDialog"] = {}
        # task-2142: per-tmux-session-name pane census cache, keyed by the same
        # `row["name"]` the rest of the Running machinery already uses. Persists
        # across refresh_running_tab calls so an unchanged pane costs zero
        # capture-pane invocations (window_activity alone tells us it changed).
        self._pane_activity_cache: dict[str, str] = {}
        self._pane_block_cache: dict[str, str] = {}
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
        # The first-ever "Manage group" dialog opened in a run visibly lags
        # (~1s) before Qt's warmed up; every dialog after that is instant.
        # Data isn't the cause - refresh() above already scans every
        # transcript ManageGroupDialog would. Paying that one-time Qt
        # widget-construction/layout cost here, off-screen, means the
        # user's first real click never has to.
        QTimer.singleShot(0, self._prewarm_manage_group_dialog)
        # Live session status, unlike usage, is cheap (local file reads) and
        # meant to feel live - refresh_running_tab never touches refresh_usage.
        self._codex_notify_warned = False
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._on_running_status_tick)
        self._status_timer.start()

    def _running_tab_visible(self) -> bool:
        """task-2142: the tmux census (session-level list-sessions AND the pane-level
        activity/capture below) only earns its cost while a human could actually see
        the result -- window minimized or another tab selected means nobody is
        looking, so skip the whole tick rather than just the capture step."""
        return (
            not self.isMinimized()
            and self.main_tabs.currentIndex() == self.main_tabs.indexOf(self.running_page)
        )

    def _on_running_status_tick(self) -> None:
        if self._running_tab_visible():
            self.refresh_running_tab()

    def _on_main_tab_changed(self, _index: int) -> None:
        # Refresh immediately on becoming visible instead of leaving the table up to
        # 2s stale after a tab switch -- cheap (one refresh), and it's exactly the
        # moment a capture is now worth paying for.
        if self._running_tab_visible():
            self.refresh_running_tab()

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
        # PROVIDERS lists Codex first (update_new_provider_list's own
        # fallback comment already says so), but addItems() alone leaves
        # index 0 - Codex - selected, and that selection is "current" by the
        # time update_new_provider_list() runs right after build_ui(), so its
        # Claude fallback (only reached when nothing matches) never actually
        # fires on a fresh launch. Set the real default here instead.
        claude_index = self.new_provider.findText("Claude")
        if claude_index >= 0:
            self.new_provider.setCurrentIndex(claude_index)
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

        usage_compact = QHBoxLayout()
        usage_compact.setSpacing(10)
        for provider in PROVIDERS:
            label = QLabel(provider)
            label.setStyleSheet("font-size: 11px; color: #aaa;")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedSize(60, 8)
            bar.setToolTip("Loading…")
            usage_compact.addWidget(label)
            usage_compact.addWidget(bar)
            self.usage_compact_labels[provider] = label
            self.usage_compact_bars[provider] = bar
        usage_compact.addStretch(1)
        self.usage_expand_button = QPushButton("Expand")
        self.usage_expand_button.setCheckable(True)
        self.usage_expand_button.toggled.connect(self.set_usage_expanded)
        usage_compact.addWidget(self.usage_expand_button)
        layout.addLayout(usage_compact)

        usage_frame = QFrame()
        usage_frame.setFrameShape(QFrame.Shape.StyledPanel)
        usage_frame.setVisible(False)
        self.usage_detail_frame = usage_frame
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
                else ("Weekly", "5-hour", "Weekly")
                if provider == "Codex"
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
            {
                "Agent": 90, "Model": 90, "Name": 220, "Working directory": 320,
                "Last updated": 140,
            },
            stretch_column="Session ID",
        )
        # _v2: task-2136 reverted All Sessions from the rejected eight-column
        # (Status/Last message added) layout back to six - an old eight-column
        # blob restored onto a six-column header scrambles widths/order (the
        # bug this reverts), so the settings key changes too, exactly like
        # ManageGroupDialog's own group_table_columns_v2 bump for the same
        # reason. A pre-existing six-column blob under the old key is simply
        # not re-read; only a fresh v2 blob round-trips.
        restore_column_widths(self.table, self.settings().get("main_table_columns_v2"))
        self.table.doubleClicked.connect(self.resume_selected)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.resume_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)

        all_sessions_page = QWidget()
        all_sessions_layout = QVBoxLayout(all_sessions_page)
        all_sessions_layout.setContentsMargins(0, 0, 0, 0)
        all_sessions_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel()
        actions.addWidget(self.status, 1)
        for label, slot in (
            ("Rename", self.rename_selected),
            ("Change directory", self.change_directory),
            ("Delete", self.delete_selected),
            ("Continue with other agent", self.continue_with_other_agent),
            ("Resume in new terminal", self.resume_selected),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            if label.startswith("Resume"):
                button.setDefault(True)
            if label == "Continue with other agent":
                self.continue_with_other_button = button
            actions.addWidget(button)
        all_sessions_layout.addLayout(actions)

        running_page = QWidget()
        running_layout = QVBoxLayout(running_page)
        running_layout.setContentsMargins(0, 0, 0, 0)
        self.running_table = QTableWidget(0, 3)
        self.running_table.setHorizontalHeaderLabels(["Name", "Status", "Last message"])
        self.running_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.running_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.running_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.running_table.setAlternatingRowColors(True)
        self.running_table.verticalHeader().setVisible(False)
        self.running_table.horizontalHeader().setStretchLastSection(True)
        self.running_table.cellDoubleClicked.connect(self.reveal_running_row)
        self.running_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.running_table.customContextMenuRequested.connect(self.running_context_menu)
        running_layout.addWidget(self.running_table, 1)
        running_actions = QHBoxLayout()
        running_actions.addStretch(1)
        stop_button = QPushButton("Stop")
        stop_button.clicked.connect(self.stop_selected_running)
        running_actions.addWidget(stop_button)
        running_layout.addLayout(running_actions)

        tabs = QTabWidget()
        tabs.addTab(all_sessions_page, "All Sessions")
        tabs.addTab(running_page, "Running")
        self.main_tabs = tabs
        self.running_page = running_page
        tabs.currentChanged.connect(self._on_main_tab_changed)
        layout.addWidget(tabs, 1)
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
            latest["settings"]["main_table_columns_v2"] = column_widths_state(self.table)
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
            if provider in self.usage_compact_labels:
                self.usage_compact_labels[provider].setVisible(enabled)
                self.usage_compact_bars[provider].setVisible(enabled)

    def set_usage_expanded(self, expanded: bool) -> None:
        """Transient only - never written to settings, so every fresh
        launch (or window reload) starts back in the compact view."""
        self.usage_detail_frame.setVisible(expanded)
        self.usage_expand_button.setText("Collapse" if expanded else "Expand")

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
            # PROVIDERS lists Codex first, but Claude is the default agent -
            # fall back to it over plain index 0 when nothing was selected before.
            default_idx = self.new_provider.findText("Claude")
            self.new_provider.setCurrentIndex(default_idx if default_idx >= 0 else 0)
        # Handoffs need a second enabled agent to hand off to.
        multiple_agents = len(enabled_providers) > 1
        self.continue_with_other_button.setVisible(multiple_agents)

    def open_settings(self) -> None:
        was_enabled = bool(self.settings().get("status_hooks_enabled", False))
        dialog = SettingsDialog(self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.metadata["settings"] = dialog.values()
            write_metadata(self.metadata)
            self.purge_expired_trash()
            self.update_usage_visibility()
            self.update_new_provider_list()
            if was_enabled and not self.settings().get("status_hooks_enabled", False):
                self.uninstall_status_hooks_everywhere()
            self.refresh()

    def uninstall_status_hooks_everywhere(self) -> None:
        """Best-effort cleanup when the status-hooks Settings toggle is
        turned back off: strips exactly the hook entries install_status_hooks
        added, from every project directory Session Hub knows about."""
        dirs = {Path(cwd) for cwd in self.metadata.get("groups", {})}
        dirs.update(Path(session.cwd) for session in self.sessions if session.cwd)
        for project_dir in dirs:
            uninstall_status_hooks(project_dir)
        uninstall_status_hooks_codex()

    def _prewarm_manage_group_dialog(self) -> None:
        """Construct-and-discard a ManageGroupDialog off-screen at startup.

        WA_DontShowOnScreen still runs the dialog through Qt's real show/
        layout/paint machinery without it ever appearing, so the one-time
        cost lands here instead of on the user's first real "Manage" click.
        No-op if there are no groups yet to build one against.
        """
        groups = self.metadata.get("groups", {})
        if not groups:
            return
        dialog = ManageGroupDialog(self, next(iter(groups)), self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        dialog.close()
        dialog.deleteLater()

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
            if provider in self.usage_compact_bars:
                compact_bar = self.usage_compact_bars[provider]
                compact_bar.setValue(0)
                compact_bar.setStyleSheet("")
                compact_bar.setToolTip("Loading…")
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
        # The Claude "Weekly (Fable)" row is optional: it only shows while
        # `/usage` still reports a Fable window. Once Fable becomes
        # credit-only and drops out of the output, the row hides itself instead
        # of showing "Unavailable" forever. Codex's rows 2-3 (either the
        # account's own 5-hour window, or a per-model breakdown) are optional
        # the same way: not every plan reports them.
        if provider == "Claude":
            optional_indices = {2}
        elif provider == "Codex":
            optional_indices = {1, 2}
        else:
            optional_indices = set()
        header = self.usage_headers[provider]
        banked = next(
            (
                w
                for w in windows
                if isinstance(w, UsageWindow) and w.count is not None
            ),
            None,
        )
        if banked:
            plural = "" if banked.count == 1 else "s"
            header.setText(
                f"<b>{provider} usage</b> · {banked.count} banked reset{plural} available"
            )
        else:
            header.setText(f"<b>{provider} usage</b>")
        if error:
            for index, (_, bar, detail) in enumerate(rows):
                if index in optional_indices:
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
                if not activity or index in optional_indices:
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
                    if index in optional_indices:
                        self.set_usage_row_visible(rows[index], False)
                        continue
                    bar.setFormat("Unavailable")
                    detail.setText("")
                    continue
                if index in optional_indices:
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
        self._sync_usage_compact(provider, error)

    def _sync_usage_compact(self, provider: str, error: str) -> None:
        """Mirror the worst (most-used) visible window onto the compact
        one-line bar; the reset/pace detail lives in its tooltip instead of
        the always-visible full grid this replaces."""
        compact_bar = self.usage_compact_bars.get(provider)
        if compact_bar is None:
            return
        worst: tuple[int, str] | None = None
        tooltip_lines = []
        for label, bar, detail in self.usage_widgets[provider]:
            if bar.isHidden():
                continue
            if "% left" in bar.format():
                remaining = bar.value()
                if worst is None or remaining < worst[0]:
                    worst = (remaining, bar.styleSheet())
                detail_text = detail.text().replace("\n", " · ")
                tooltip_lines.append(f"{label.text()}: {bar.format()} ({detail_text})")
            elif "requests" in bar.format():
                tooltip_lines.append(f"{label.text()}: {bar.format()} ({detail.text()})")
        if worst is not None:
            compact_bar.setValue(worst[0])
            compact_bar.setStyleSheet(worst[1])
        elif error:
            compact_bar.setValue(0)
            compact_bar.setStyleSheet("")
        compact_bar.setToolTip("\n".join(tooltip_lines) if tooltip_lines else (error or "Unavailable"))

    @staticmethod
    def set_usage_row_visible(
        row: tuple[QLabel, QProgressBar, QLabel], visible: bool
    ) -> None:
        for widget in row:
            widget.setVisible(visible)

    def refresh_all(self) -> None:
        self.refresh()
        self.refresh_usage()

    def group_cwd_for_session_key(self, session_key: str | None) -> str | None:
        """Recover a session group's cwd from either shape of group-linked key.

        Two different keys both mean "this belongs to a saved session group":
        a row's own override_key ("group:{cwd}#{name}", minted by
        register_group_row) and the main listview's pseudo-row native key
        ("{provider}:group:{cwd}", built in discover_sessions for the
        group's own collapsed row). Both need to resolve to the same
        group-level launch options.
        """
        if not session_key:
            return None
        if session_key.startswith("group:") and "#" in session_key:
            cwd = session_key[len("group:"):].rsplit("#", 1)[0]
        elif ":group:" in session_key:
            cwd = session_key.split(":group:", 1)[1]
        else:
            return None
        return cwd if cwd in self.metadata.get("groups", {}) else None

    def group_launch_options(self, session_key: str | None) -> tuple[dict, dict]:
        """(env, flags) saved on the session group `session_key` belongs to, if any."""
        cwd = self.group_cwd_for_session_key(session_key)
        if not cwd:
            return {}, {}
        group = self.metadata.get("groups", {}).get(cwd) or {}
        return group.get("env") or {}, group.get("flags") or {}

    def effective_tmux(self, explicit: bool | None) -> bool:
        """Resolve a per-session/per-group tmux override against the global default.

        `explicit` is whatever a "tmux" key currently holds: None means the
        session/group has never been toggled and should follow the global
        "launch_in_tmux" setting; True/False means it was explicitly set and
        wins outright, per-item, over the global default.
        """
        if explicit is None:
            return bool(self.settings().get("launch_in_tmux", False))
        return explicit

    def effective_model(self, session_key: str | None, provider: str = "Claude") -> str | None:
        """The model a session would (re)launch with, if any is set.

        Claude resolves through ANTHROPIC_MODEL with the same global-then-
        group-then-session-override precedence as launch_env - this is what
        the Model column (main listview and group dialog alike) displays.
        Other providers have no such env-var mechanism: Codex takes a plain
        per-row/session "model" override instead (see register_group_row);
        without a `provider` this used to assume Claude for every row,
        leaking a Claude-scoped global default onto Codex/Antigravity rows
        that never asked for one.
        """
        if provider != "Claude":
            overrides: dict = {}
            if session_key:
                overrides = (self.metadata.get("sessions") or {}).get(session_key) or {}
            return overrides.get("model") if provider == "Codex" else None
        global_env = self.settings().get("global_env") or {}
        group_env, _ = self.group_launch_options(session_key)
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        return (
            overrides.get("ANTHROPIC_MODEL")
            or group_env.get("ANTHROPIC_MODEL")
            or global_env.get("ANTHROPIC_MODEL")
            or None
        )

    def effective_account(self, session_key: str | None) -> str | None:
        """The CLAUDE_CONFIG_DIR a Claude session would (re)launch with, if set.

        Same global-then-group-then-session-override precedence as
        effective_model, storing/reading through the same env dict - see
        register_group_row. No Codex/Antigravity equivalent: the account
        concept is Claude-account-specific.
        """
        global_env = self.settings().get("global_env") or {}
        group_env, _ = self.group_launch_options(session_key)
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        return (
            overrides.get("CLAUDE_CONFIG_DIR")
            or group_env.get("CLAUDE_CONFIG_DIR")
            or global_env.get("CLAUDE_CONFIG_DIR")
            or None
        )

    def effective_codex_reasoning_effort(self, session_key: str | None) -> str | None:
        """The reasoning-effort level a Codex session would (re)launch with, if set.

        Session-only, same as effective_model's Codex path - Codex has no
        global/group env-var tier to fall back through.
        """
        overrides: dict = {}
        if session_key:
            overrides = (self.metadata.get("sessions") or {}).get(session_key) or {}
        return overrides.get("reasoning_effort")

    def populate_session_table(
        self, table: QTableWidget, sessions: list[Session], columns: tuple[str, ...]
    ) -> None:
        """Fill a QTableWidget's shared columns from `columns`.

        The common rendering both the main listview and ManageGroupDialog build
        on, so only their differences (extra columns, extra rows) need writing
        out separately.
        """
        colors = {"Codex": "#5aa9ff", "Claude": "#d977ff", "Antigravity": "#42d6c5"}
        # Only computed when a caller's own column set actually asks for it
        # (ManageGroupDialog's SHARED_COLUMNS deliberately doesn't - see its
        # own STATUS_COLUMN) - session_activity's transcript read is bounded
        # per call, but there is no reason to pay it at all for a table that
        # never shows the result.
        needs_activity = "Status" in columns or "Last message" in columns
        settings = self.metadata.get("settings", {}) if needs_activity else {}
        session_overrides = self.metadata.get("sessions", {}) or {} if needs_activity else {}
        # One tmux snapshot for the whole table, not one `tmux` subprocess per
        # row - All Sessions can list every historical session, so this is the
        # difference between one process and up to 2*N per refresh.
        live_names = tmux_live_session_names() if needs_activity else None
        table.setSortingEnabled(False)
        table.setRowCount(len(sessions))
        for row, session in enumerate(sessions):
            activity = ("unknown", "")
            if needs_activity and not self.is_group_session(session):
                tmux_enabled, tmux_name, _ = standalone_tmux_status(
                    session, session_overrides.get(session.key, {}), settings, live_names
                )
                activity = session_activity(
                    session, tmux_enabled=tmux_enabled, tmux_name=tmux_name, live_names=live_names
                )
            for col, column in enumerate(columns):
                if column == "Agent":
                    # A collapsed group's own summary row spans every member's
                    # provider (see pseudo_provider in discover_sessions) -
                    # showing one of them as if the whole group were that
                    # agent is misleading, so it gets its own label instead.
                    if self.is_group_session(session):
                        item = QTableWidgetItem("Group")
                    else:
                        item = QTableWidgetItem(session.provider)
                        item.setForeground(QColor(colors.get(session.provider, "#ffffff")))
                elif column == "Model":
                    item = QTableWidgetItem(
                        ""
                        if self.is_group_session(session)
                        else self.effective_model(session.key, session.provider) or "Default"
                    )
                elif column == "Name":
                    item = QTableWidgetItem(session.title)
                elif column == "Status":
                    label, color = activity_label(activity[0])
                    item = QTableWidgetItem(label)
                    item.setForeground(QColor(color))
                elif column == "Last message":
                    detail = " ".join(activity[1].split())
                    item = QTableWidgetItem(detail if len(detail) <= 80 else detail[:79] + "…")
                    if detail:
                        item.setToolTip(detail)
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
        self.refresh_running_tab()

    def refresh_running_tab(self) -> None:
        """Flat list of every currently-running tmux group row, across every project.

        Reuses group_row_status/tmux_session_alive - the exact same signal
        ManageGroupDialog now shows per-group, just flattened across all of
        them here.
        """
        settings = self.metadata.get("settings", {})
        live: list[Session] = []
        if settings.get("enable_codex", True):
            live += codex_sessions()
        if settings.get("enable_claude", True):
            live += claude_sessions()
        if settings.get("enable_antigravity", True):
            live += antigravity_sessions()

        # One tmux snapshot for the whole refresh - every group row and every
        # standalone session below shares it instead of each spawning its own
        # `tmux has-session` (was up to 2 subprocesses per row).
        live_names = tmux_live_session_names()

        running: list[tuple[str, str, dict, Session | None]] = []
        claimed: set[str] = set()
        for cwd, group in self.metadata.get("groups", {}).items():
            tmux_enabled = self.effective_tmux(group.get("tmux"))
            if not tmux_enabled:
                continue
            display_name = group.get("display_name") or Path(cwd).name or cwd
            for row in group.get("rows", []):
                match = find_group_member_session(row, cwd, live, frozenset(claimed))
                if match:
                    claimed.add(match.native_key)
                if group_row_status(row, match, tmux_enabled, live_names) == "Running":
                    running.append((display_name, cwd, row, match))

        session_overrides = self.metadata.get("sessions", {}) or {}
        for session in self.sessions:
            if session.session_id.startswith("group:"):
                continue
            tmux_enabled, name, status = standalone_tmux_status(
                session, session_overrides.get(session.key, {}), settings, live_names
            )
            if tmux_enabled and status == "Running":
                running.append((
                    session.title, session.cwd,
                    {"name": name, "provider": session.provider}, session,
                ))

        # task-2142: Last-message census. One list-panes call for every row's
        # window_activity; only names whose activity changed since the cached value
        # get captured, and every changed name is captured in ONE batched tmux
        # invocation (capture_changed_panes), not one spawn per row.
        pane_snapshot = tmux_pane_activity_snapshot()
        changed_pane_ids: dict[str, str] = {}
        for _dn, _cwd, row, _m in running:
            entry = pane_snapshot.get(row["name"])
            if not entry:
                continue
            pane_id, activity = entry
            if self._pane_activity_cache.get(row["name"]) != activity:
                changed_pane_ids[row["name"]] = pane_id
        captured = capture_changed_panes(changed_pane_ids)
        for name, raw_text in captured.items():
            self._pane_block_cache[name] = extract_last_meaningful_block(raw_text)
            self._pane_activity_cache[name] = pane_snapshot[name][1]

        name_counts = collections.Counter(row["name"] for _dn, _cwd, row, _m in running)
        self.running_table.setRowCount(len(running))
        for index, (display_name, cwd, row, match) in enumerate(running):
            session_id = match.session_id if match else None
            provider = row.get("provider", "Claude")
            name_item = QTableWidgetItem(
                row["name"]
                if name_counts[row["name"]] <= 1
                # A visible name collision (same row name, different project) gets a
                # parenthetical project suffix so the two rows are distinguishable at
                # a glance - full identity (provider/project/cwd) lives in the tooltip
                # rather than cluttering every row with it.
                else f"{row['name']}  ({display_name})"
            )
            name_item.setData(Qt.ItemDataRole.UserRole, (cwd, row["name"], session_id))
            name_item.setToolTip(f"{provider} · {display_name} · {cwd}")
            self.running_table.setItem(index, 0, name_item)
            # Every row here is already confirmed tmux-alive (group_row_status/
            # standalone_tmux_status above), so session_activity's own liveness
            # check always passes - it still goes through the one shared verdict
            # function rather than reading status files directly (see its
            # docstring), so this can never diverge from the All Sessions/TUI/
            # JSON verdicts for the same session. Read-only: refresh must never
            # write status itself (status_pipeline_plan.md's contract) - the
            # only writer is the hook pipeline (write_session_status).
            state, _detail = (
                session_activity(match, tmux_enabled=True, tmux_name=row["name"], live_names=live_names)
                if match else ("unknown", "")
            )
            self.running_table.setItem(index, 1, self._status_column_item(state))
            # Last message is the latest meaningful terminal-output block (the pane
            # census above), never session_activity's status detail -- the brief's
            # explicit distinction (task-2142).
            last_message = self._pane_block_cache.get(row["name"], "")
            self.running_table.setItem(index, 2, self._detail_column_item(last_message))

    def _status_column_item(self, state: str) -> QTableWidgetItem:
        label, color = activity_label(state)
        item = QTableWidgetItem(label)
        item.setForeground(QColor(color))
        return item

    @staticmethod
    def _detail_column_item(detail: str) -> QTableWidgetItem:
        detail = " ".join(detail.split())
        snippet = detail if len(detail) <= 80 else detail[:79] + "…"
        item = QTableWidgetItem(snippet)
        if detail:
            item.setToolTip(detail)
        return item

    def stop_selected_running(self) -> None:
        row = self.running_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Session Hub", "Select a running session first.")
            return
        item = self.running_table.item(row, 0)
        cwd, name, _session_id = item.data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(
            self,
            "Stop session",
            f"Stop {name!r}? This ends its tmux session; unsaved terminal state is lost.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        stop_tmux_session(name)
        self.refresh_running_tab()

    def _focus_or_resume_session(self, cwd: str, name: str, session_id: str | None) -> None:
        """Bring a running row's terminal to the front, or open/resume it if
        no terminal window exists yet. The one focus authority behind both
        the Running double-click and Recent-activity activation
        (task-2135) - reusing it, rather than a second implementation, is
        what keeps them from ever disagreeing about what a given identity
        resolves to.

        wmctrl -a already unminimizes as well as raising, so a window that's
        merely minimized in the taskbar is covered for free. wmctrl can only
        raise a window that already exists, though - it can't materialize
        one that was never opened (session launched outside Session Hub, or
        its terminal was closed while tmux kept running headless). For that
        case, reuse the exact same launch_group_row/resume_session_by_name
        calls the "All Sessions" double-click already uses successfully -
        has-session gates their tmux attach, so this only ever opens a
        terminal onto the existing session, never a second copy of it.
        """
        if session_id:
            status = read_session_status(session_id)
            if status and status.get("state") == "done":
                write_session_status(session_id, "idle", status.get("detail", ""))
        if window_titled(name):
            threading.Thread(target=focus_window_by_title, args=(name,), daemon=True).start()
            return
        if self.metadata.get("groups", {}).get(cwd):
            result = self.launch_group_row(cwd, name)
        else:
            result = self.resume_session_by_name(name)
        if result.get("status") == "error":
            QMessageBox.critical(self, "Could not open session", result["message"])
            return
        # tmux_group_launch_command never passes gnome-terminal a --title=,
        # so spawn()'s own focus branch (which only fires for a --title=
        # command) never triggers for it - the new window's stacking is
        # left entirely up to GNOME, which is not reliable enough to count
        # on with several other windows open. Same wmctrl activation used
        # above for an already-open window, just given time to appear first.
        threading.Thread(target=focus_window_by_title, args=(name,), daemon=True).start()

    def reveal_running_row(self, row: int, _column: int = 0) -> None:
        """Double-click a Running row: bring its terminal to the front."""
        item = self.running_table.item(row, 0)
        if not item:
            return
        cwd, name, session_id = item.data(Qt.ItemDataRole.UserRole)
        self._focus_or_resume_session(cwd, name, session_id)

    def running_context_menu(self, point) -> None:
        """Right-click a Running row: the same exact-identity focus/stop
        authority as double-click and the Stop button, just reachable
        without a prior left-click select."""
        item = self.running_table.itemAt(point)
        if item is None:
            return
        row = item.row()
        self.running_table.setCurrentCell(row, 0)
        menu = QMenu(self)
        bring_up_action = QAction("Bring up window", self)
        bring_up_action.triggered.connect(lambda: self.reveal_running_row(row))
        menu.addAction(bring_up_action)
        stop_action = QAction("Stop session", self)
        stop_action.triggered.connect(self.stop_selected_running)
        menu.addAction(stop_action)
        menu.exec(self.running_table.viewport().mapToGlobal(point))

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
        """Merge global + group + per-session env overrides onto the current process env.

        Returns None when nothing is configured or stripped so the launched
        process simply inherits Session Hub's environment as-is. Per-session
        values win over the session's group, which wins over global. `strip`
        removes inherited keys outright (e.g. CLAUDE_CODE_CHILD_SESSION, which
        Session Hub itself may have inherited if it was launched from inside
        another Claude session, and which disables transcript saving in
        whatever it's launched into).
        """
        global_env = self.settings().get("global_env") or {}
        group_env, _ = self.group_launch_options(session_key)
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        combined: dict[str, str] = {}
        for source in (global_env, group_env, overrides):
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
        """Merge global + group + per-session CLI flag overrides into argv fragments.

        Per-session values win over the session's group, which wins over
        global, matching launch_env's precedence. `extra` wins over all three
        -- it carries a one-off choice made in the launch dialog, which has
        no session key yet because the session does not exist.
        """
        global_flags = self.settings().get("global_flags") or {}
        _, group_flags = self.group_launch_options(session_key)
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("flags") or {}
        combined: dict[str, str] = {}
        for source in (global_flags, group_flags, overrides, extra or {}):
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
        model: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        env = self.launch_env(session_key, strip=strip_env)
        if extra_env:
            env = {**(env or os.environ), **extra_env}
        subprocess.Popen(
            command,
            start_new_session=True,
            env=env,
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
                capture_hub_launch(pidfile, cwd, session_id, model)
            else:
                threading.Thread(
                    target=capture_hub_launch,
                    args=(pidfile, cwd, session_id, model),
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
        group_env, group_flags = self.group_launch_options(session.key)
        dialog = SessionLaunchOptionsDialog(
            session.title,
            {**(self.settings().get("global_env") or {}), **group_env},
            env_overrides,
            {**(self.settings().get("global_flags") or {}), **group_flags},
            flag_overrides,
            self,
            show_tmux=not self.is_group_session(session),
            tmux_override=existing.get("tmux"),
            provider=session.provider,
            model=existing.get("model"),
            reasoning_effort=existing.get("reasoning_effort"),
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
            if session.provider == "Codex":
                model = dialog.model()
                if model:
                    entry["model"] = model
                else:
                    entry.pop("model", None)
                effort = dialog.reasoning_effort()
                if effort:
                    entry["reasoning_effort"] = effort
                else:
                    entry.pop("reasoning_effort", None)
            tmux = dialog.tmux()
            if tmux is None:
                entry.pop("tmux", None)
            else:
                entry["tmux"] = tmux
            write_metadata(self.metadata)
            self.refresh()

    def edit_group_launch_options(self, cwd: str) -> None:
        """Edit the env/flag overrides applied to every row in the group at `cwd`.

        Sits between global settings and a row's own launch options in
        precedence (see launch_env/launch_flags/effective_model) - a row
        that sets its own override still wins over this.
        """
        group = self.metadata.get("groups", {}).get(cwd)
        if not group:
            return
        name = group.get("display_name") or Path(cwd).name or cwd
        dialog = SessionLaunchOptionsDialog(
            name,
            self.settings().get("global_env") or {},
            group.get("env") or {},
            self.settings().get("global_flags") or {},
            group.get("flags") or {},
            self,
            scope="this group",
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            env = dialog.env()
            if env:
                group["env"] = env
            else:
                group.pop("env", None)
            flags = dialog.flags()
            if flags:
                group["flags"] = flags
            else:
                group.pop("flags", None)
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
            result = self.rename_session_name(session, name.strip())
            if result["status"] == "error":
                QMessageBox.warning(self, "Rename session", result["message"])

    def rename_session_name(self, session: Session, new_name: str) -> dict:
        """Rename one (non-group) session's display name AND its live tmux
        session together, atomically.

        Before this, "Rename session" (a plain save_override("name", ...))
        never touched tmux at all - the exact live repro row447 rework
        reported: a Codex conversation renamed to "Music Download" while its
        tmux session (and therefore every peer/phone address for it) stayed
        "projects", silently split. Canonicalized the same way a group row
        name is (sanitize_tmux_session_name) and reconciled/refused the same
        way rename_group_row is - see reconcile_tmux_rename.
        """
        sanitized = sanitize_tmux_session_name(" ".join(str(new_name).strip().split()))
        if not sanitized:
            return {"status": "error", "message": "Name must not be empty"}
        overrides = (self.metadata.get("sessions") or {}).get(session.key, {})
        tmux_enabled, old_name, _status = standalone_tmux_status(
            session, overrides, self.settings()
        )
        reconciled = {"tmux_renamed": False, "error": None}
        if tmux_enabled and old_name and old_name != sanitized:
            reconciled = reconcile_tmux_rename(old_name, sanitized)
            if reconciled["error"]:
                return {"status": "error", "message": reconciled["error"]}
        self.save_override(session, "name", sanitized)
        return {"status": "renamed", "name": sanitized, "tmux_renamed": reconciled["tmux_renamed"]}

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
        reasoning_effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        title = f"{provider} — {Path(cwd).name or cwd}"
        launch_cwd = source_cwd if provider in ("Claude", "Codex") and session_id else cwd
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
            command += codex_launch_args(
                cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                danger_mode=self.settings().get("codex_danger_mode", False),
                session_id=session_id,
                source_cwd=source_cwd,
                initial_prompt=initial_prompt,
            )
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
            if initial_prompt:
                claude_args += [initial_prompt]
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
            if initial_prompt:
                command += ["--prompt-interactive", initial_prompt]
        return command

    def group_env_overrides(self, session_key: str | None) -> dict[str, str]:
        """Just the env overrides (global + group + per-session) - no os.environ merge.

        launch_env returns the full merged environment for Popen's env=
        kwarg, which only reaches the process Session Hub directly spawns.
        A tmux-launched claude process isn't that process (tmux daemonizes
        it, often onto an already-running server that does NOT inherit
        Popen's env=), so it needs these injected explicitly into the
        command tmux execs instead - see tmux_group_launch_command and
        launch's use_tmux branch.
        """
        global_env = self.settings().get("global_env") or {}
        group_env, _ = self.group_launch_options(session_key)
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        combined: dict[str, str] = {}
        for source in (global_env, group_env, overrides):
            for name, value in source.items():
                if str(name).strip():
                    combined[str(name)] = str(value)
        return combined

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
        use_tmux: bool = False,
        tmux_name: str | None = None,
        reasoning_effort: str | None = None,
        initial_prompt: str | None = None,
        account_config_dir: str | None = None,
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
        if use_tmux and self.settings().get("status_hooks_enabled", False):
            if provider == "Claude":
                install_status_hooks(Path(cwd))
            elif provider == "Codex" and not install_status_hooks_codex() and not self._codex_notify_warned:
                self._codex_notify_warned = True
                QMessageBox.warning(
                    self,
                    "Codex live status not installed",
                    "~/.codex/config.toml already has a `notify` command that isn't "
                    "Session Hub's own, so it was left alone - Codex rows won't show "
                    "live status until you clear or replace that `notify` line yourself.",
                )
        try:
            if use_tmux and provider in ("Claude", "Codex"):
                # tmux_name, not flag_overrides["--name"]: resuming a group
                # row (session_id set) never passes --name at all - Claude
                # already knows which conversation to continue via --resume,
                # but the tmux session still needs a name to send-keys at,
                # and it must be the row's own name to match a fresh launch.
                name = tmux_name or (flag_overrides or {}).get("--name")
                if not name:
                    raise RuntimeError("Launching into tmux requires a session name.")
                # Canonicalized BEFORE launch_flags below builds claude_args -
                # Claude's own --name flag must equal the exact name
                # tmux_group_launch_command creates the tmux session under
                # (it canonicalizes independently), or the two identities
                # split the way row447 rework's live repro found: Claude
                # reports one name, the tmux/peer address is a different one.
                name = sanitize_tmux_session_name(name)
                if flag_overrides and flag_overrides.get("--name"):
                    flag_overrides = {**flag_overrides, "--name": name}
            flags = (
                self.launch_flags(session_key, flag_overrides)
                if provider == "Claude"
                else []
            )
            if use_tmux and provider in ("Claude", "Codex"):
                if provider == "Codex":
                    # Codex has no --name; the tmux session name IS its address
                    # (VAMPULSE peers reach it with `session_ctl.py send <name>`).
                    # Before 2026-08-23 this branch was Claude-only, so a Codex
                    # row with "Launch in tmux" checked silently fell through to a
                    # plain gnome-terminal and tmux could not see it.
                    claude_args = codex_launch_args(
                        cwd,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        danger_mode=self.settings().get("codex_danger_mode", False),
                        session_id=session_id,
                        source_cwd=source_cwd,
                        initial_prompt=initial_prompt,
                    )
                else:
                    claude_args = [executable("claude")]
                    if self.settings().get("claude_danger_mode", False):
                        claude_args += ["--dangerously-skip-permissions"]
                    if model:
                        claude_args += ["--model", model]
                    claude_args += flags
                    if session_id:
                        claude_args += ["--resume", session_id]
                    if initial_prompt:
                        claude_args += [initial_prompt]
                env_overrides = self.group_env_overrides(session_key)
                if account_config_dir:
                    env_overrides = {**env_overrides, "CLAUDE_CONFIG_DIR": account_config_dir}
                claude_args = prefix_env_command(claude_args, env_overrides, strip_env)
                command = tmux_group_launch_command(name, cwd, claude_args)
                self.spawn(
                    command, session_key, cwd=cwd, focus=focus, strip_env=strip_env
                )
                return
            pidfile = new_pid_capture_file() if provider == "Claude" else None
            self.spawn(
                self.terminal_command(
                    provider, session_id, cwd, source_cwd, model, flags, pidfile,
                    reasoning_effort=reasoning_effort, initial_prompt=initial_prompt,
                ),
                session_key,
                pidfile=pidfile,
                cwd=cwd,
                session_id=session_id,
                focus=focus,
                strip_env=strip_env,
                wait_for_tracking=wait_for_tracking,
                model=model,
                extra_env={"CLAUDE_CONFIG_DIR": account_config_dir} if account_config_dir else None,
            )
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "Could not launch session", str(error))

    def resume_selected(self) -> None:
        session = self.selected()
        if not session:
            return
        self.resume_session(session)

    def resume_session(
        self, session: Session, *, wait_for_tracking: bool = False
    ) -> None:
        if self.is_group_session(session):
            self.manage_group()
            return
        overrides = (self.metadata.get("sessions") or {}).get(session.key, {})
        self.launch(
            session.provider,
            session.session_id,
            session.cwd,
            session.source_cwd,
            # Codex has no persistent env-var equivalent to ANTHROPIC_MODEL -
            # its model override only takes effect if threaded through as an
            # explicit -m/--model argv, so it has to be looked up and passed
            # here explicitly, unlike Claude's (which rides launch_env's
            # os.environ merge with no extra wiring).
            model=self.effective_model(session.key, session.provider)
            if session.provider == "Codex"
            else None,
            reasoning_effort=self.effective_codex_reasoning_effort(session.key)
            if session.provider == "Codex"
            else None,
            session_key=session.key,
            # Strip the inherited child-session marker, as launch_group_row already
            # does. A single session has no `transcripts` toggle, so it is always on
            # and the marker is always wrong to pass down. Latent until --resume-session
            # made "Session Hub launched from inside a Claude session" a normal path:
            # the Hub inherits the marker there and every session it opens starts with
            # transcript saving off.
            strip_env=["CLAUDE_CODE_CHILD_SESSION"],
            wait_for_tracking=wait_for_tracking,
            use_tmux=self.effective_tmux(overrides.get("tmux")),
            # --name is Claude-only; a Codex row's address is its display name
            # (the Hub rename, e.g. VAMP-worker4), else the session title.
            tmux_name=(overrides.get("flags") or {}).get("--name")
            or overrides.get("name")
            or session.title,
        )

    def resume_session_by_name(
        self, wanted: str, *, wait_for_tracking: bool = False
    ) -> dict:
        """Find ONE non-group session by Hub name, key or raw id, and resume it.

        The single-session counterpart to launch_group_row, shared by the
        --resume-session CLI flag. Matching checks the Hub's custom name
        first, because the name in the Hub's list is what the user reads and
        types; the transcript title and the raw ids are fallbacks. An
        ambiguous name is an error with the candidates listed rather than a
        guess - resuming the wrong session is not something the caller can
        see from a JSON line.
        """
        sessions = getattr(self, "sessions", None) or discover_sessions(self.metadata)
        overrides = self.metadata.get("sessions", {}) or {}
        needle = wanted.strip().lower()
        matches = []
        for session in sessions:
            custom = (overrides.get(session.key) or {}).get("name") or ""
            candidates = (custom, session.title or "", session.key, session.session_id)
            if any(c and c.lower() == needle for c in candidates):
                matches.append((custom or session.title, session))
        if not matches:
            return {"status": "error", "message": f"No session matching {wanted!r}"}
        if len(matches) > 1:
            return {
                "status": "error",
                "message": f"{wanted!r} matches {len(matches)} sessions -- use a key",
                "candidates": [s.key for _, s in matches],
            }
        name, session = matches[0]
        if self.is_group_session(session):
            return {
                "status": "error",
                "message": (
                    f"{wanted!r} is a session GROUP -- use "
                    f"--launch-group-row {session.cwd!r} <row-name>"
                ),
            }
        self.resume_session(session, wait_for_tracking=wait_for_tracking)
        return {
            "status": "resumed",
            "name": name,
            "key": session.key,
            "cwd": session.cwd,
        }

    def is_group_session(self, session: Session) -> bool:
        return session.session_id.startswith("group:")

    def manage_group(self) -> None:
        session = self.selected()
        if not session or not self.is_group_session(session):
            return
        if session.cwd not in self.metadata.get("groups", {}):
            return
        existing = self.group_dialogs.get(session.cwd)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        # show(), not exec(): the main window must stay interactable while
        # this is open (e.g. to launch other sessions, or manage a second
        # group at the same time) - exec() is application-modal and blocks
        # everything else. WA_DeleteOnClose plus the finished signal keep
        # group_dialogs from accumulating closed dialogs.
        dialog = ManageGroupDialog(self, session.cwd, self)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _: self.group_dialogs.pop(session.cwd, None))
        self.group_dialogs[session.cwd] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

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

    def change_group_directory(self) -> None:
        """Move a saved group to a new working directory.

        A group's cwd is the literal key of metadata["groups"] (unlike an
        individual session's cwd, which is just an override) - so this
        rekeys the group dict itself, then sets the same per-session cwd
        override on every member row that change_directory_for would set
        on an individual session. Like that action, this only edits
        metadata: an already-running member keeps its own real working
        directory until it's next resumed.
        """
        session = self.selected()
        if not session or not self.is_group_session(session):
            return
        groups = self.metadata.get("groups", {})
        old_cwd = session.cwd
        group = groups.get(old_cwd)
        if not group:
            return
        start = old_cwd if Path(old_cwd).is_dir() else str(HOME)
        new_cwd = QFileDialog.getExistingDirectory(self, "Working directory", start)
        if not new_cwd or new_cwd == old_cwd:
            return
        if new_cwd in groups:
            QMessageBox.warning(
                self,
                "Group already exists",
                f"A group already exists at “{new_cwd}”.",
            )
            return
        group["cwd"] = new_cwd
        groups[new_cwd] = groups.pop(old_cwd)
        overrides = self.metadata.setdefault("sessions", {})
        for row in group.get("rows", []):
            overrides.setdefault(row["override_key"], {})["cwd"] = new_cwd
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
        settings = self.settings()
        live_sessions: list[Session] = []
        if settings.get("enable_claude", True):
            live_sessions += claude_sessions()
        if settings.get("enable_codex", True):
            live_sessions += codex_sessions()
        if settings.get("enable_antigravity", True):
            live_sessions += antigravity_sessions()
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
        copy_dialog = TranscriptPathDialog(session, target, self)
        if copy_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        clipboard_text = str(session.path)
        if copy_dialog.include_prompt:
            clipboard_text += "\n\n" + TRANSCRIPT_READ_PROMPT
        QApplication.clipboard().setText(clipboard_text)

        group_cwd = self.group_cwd_for_session_key(session.key)
        group_row = None
        if group_cwd:
            group_row = next(
                (
                    row
                    for row in self.metadata["groups"][group_cwd]["rows"]
                    if row.get("override_key") == session.key
                ),
                None,
            )
        if group_row is not None:
            use_tmux = self.effective_tmux(self.metadata["groups"][group_cwd].get("tmux"))
            tmux_name = group_row["name"]
        else:
            overrides = (self.metadata.get("sessions") or {}).get(session.key, {})
            use_tmux = self.effective_tmux(overrides.get("tmux"))
            tmux_name = (
                (overrides.get("flags") or {}).get("--name")
                or overrides.get("name")
                or session.title
            )

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
            # Swapping back to a destination already launched once before -
            # reuse whatever model/effort it was already launched with
            # rather than asking again (resume_session's own convention,
            # session_hub.py:5667-5672). A group row's model/effort is
            # always written onto its override_key (the group_row sync
            # write further below, and register_group_row's own
            # convention elsewhere) - existing_target.key is the raw
            # native provider key instead, a different identity that
            # never receives that write, so a group session must be
            # looked up by override_key or the stored value never
            # round-trips on the next swap back.
            lookup_key = (
                group_row["override_key"] if group_row is not None else existing_target.key
            )
            model = (
                self.effective_model(lookup_key, target)
                if target != "Claude"
                else None
            )
            reasoning_effort = (
                self.effective_codex_reasoning_effort(lookup_key)
                if target == "Codex"
                else None
            )
            # account_config_dir stays None here too, same reason model
            # does for Claude: it already round-trips via CLAUDE_CONFIG_DIR
            # stored on lookup_key, resolved automatically by
            # group_env_overrides at launch time below.
            account_config_dir = None
        else:
            # No linked session for `target` yet to reuse a model/effort
            # from - fall back to whatever this group/session is already
            # configured to launch `target` with (same source
            # effective_model/effective_codex_reasoning_effort read the
            # existing_target branch above), so the dialog preselects it
            # instead of always defaulting to "Default".
            default_model = self.effective_model(session.key, target)
            default_reasoning_effort = (
                self.effective_codex_reasoning_effort(session.key)
                if target == "Codex"
                else None
            )
            default_account = (
                self.effective_account(session.key) if target == "Claude" else None
            )
            dialog = AgentModelEffortDialog(
                target,
                self,
                default_model=default_model,
                default_reasoning_effort=default_reasoning_effort,
                claude_accounts=self.settings().get("claude_accounts") or DEFAULT_CLAUDE_ACCOUNTS,
                default_account=default_account,
                accounts_enabled=bool(self.settings().get("claude_accounts_enabled")),
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            model = dialog.model
            reasoning_effort = dialog.reasoning_effort
            account_config_dir = dialog.account_config_dir if target == "Claude" else None

        if use_tmux:
            stop_tmux_session(tmux_name)

        if existing_target:
            link["active"] = existing_target.native_key
            self.launch(
                target,
                existing_target.session_id,
                session.cwd,
                source_cwd=existing_target.source_cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                # lookup_key (not existing_target.key): launch_env/
                # launch_flags/group_launch_options resolve group-tier
                # and session-tier env/flag overrides by override_key
                # for a group row - the native key finds neither, so a
                # Claude reuse (model=None above, relying entirely on
                # the ANTHROPIC_MODEL env var) silently lost its model
                # and launched with the CLI's bare default instead.
                session_key=lookup_key,
                use_tmux=use_tmux,
                tmux_name=tmux_name,
            )
        elif target == "Claude":
            target_id = str(uuid.uuid4())
            target_key = f"Claude:{target_id}"
            link["members"].append(target_key)
            link["active"] = target_key
            if model:
                self.metadata.setdefault("sessions", {}).setdefault(
                    target_key, {}
                ).setdefault("env", {})["ANTHROPIC_MODEL"] = model
            if account_config_dir:
                self.metadata.setdefault("sessions", {}).setdefault(
                    target_key, {}
                ).setdefault("env", {})["CLAUDE_CONFIG_DIR"] = account_config_dir
            self.launch(
                target,
                None,
                session.cwd,
                model=model,
                session_key=target_key,
                flag_overrides={"--name": session.title, "--session-id": target_id},
                use_tmux=use_tmux,
                tmux_name=tmux_name,
            )
        else:
            provider_sessions = (
                codex_sessions() if target == "Codex" else antigravity_sessions()
            )
            existing = [item.native_key for item in provider_sessions]
            self.metadata.setdefault("pending_links", []).append(
                {
                    "logical_key": logical_key,
                    "target_provider": target,
                    "existing_keys": existing,
                    "cwd": session.cwd,
                    "started_ms": int(datetime.now().timestamp() * 1000) - 1000,
                    "expires_ms": int(datetime.now().timestamp() * 1000)
                    + 15 * 60 * 1000,
                    # No session id exists yet to key this model/effort
                    # choice onto - resolve_pending_links writes it onto
                    # the real native key once discovered, so a later swap
                    # back to this provider (now an existing_target) still
                    # finds it via effective_model/effective_codex_reasoning_effort.
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    # In a tmux group this is the launch identity. The Codex
                    # process under that row exposes its exact rollout via an
                    # open fd, so resolve_pending_links never has to guess
                    # among sibling Codex sessions sharing the same cwd.
                    "target_tmux_name": tmux_name if use_tmux else None,
                }
            )
            self.launch(
                target,
                None,
                session.cwd,
                model=model,
                reasoning_effort=reasoning_effort if target == "Codex" else None,
                use_tmux=use_tmux,
                tmux_name=tmux_name,
            )

        if group_row is not None:
            # The group row's own provider is what find_group_member_session
            # filters new session matches by - left stale, the merged
            # (now-target-provider) session can never be folded back into
            # the group and shows up as a separate standalone row instead.
            group_row["provider"] = target
            override_key = group_row["override_key"]
            if target == "Claude":
                if model:
                    self.metadata.setdefault("sessions", {}).setdefault(
                        override_key, {}
                    ).setdefault("env", {})["ANTHROPIC_MODEL"] = model
                if account_config_dir:
                    self.metadata.setdefault("sessions", {}).setdefault(
                        override_key, {}
                    ).setdefault("env", {})["CLAUDE_CONFIG_DIR"] = account_config_dir
            elif target == "Codex":
                entry = self.metadata.setdefault("sessions", {}).setdefault(
                    override_key, {}
                )
                if model:
                    entry["model"] = model
                if reasoning_effort:
                    entry["reasoning_effort"] = reasoning_effort

        write_metadata(self.metadata)
        QTimer.singleShot(2500, self.poll_pending_links)

    def poll_pending_links(self) -> None:
        self.refresh()
        if self.metadata.get("pending_links"):
            QTimer.singleShot(2500, self.poll_pending_links)

    def launch_new(self, provider: str) -> None:
        dialog = NewSessionDialog(provider, self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.directory:
            tmux_name = (
                suggest_session_name(dialog.directory, dialog.model, set())
                if dialog.use_tmux
                else None
            )
            self.launch(
                provider,
                None,
                str(dialog.directory),
                model=dialog.model,
                reasoning_effort=dialog.reasoning_effort,
                account_config_dir=dialog.account_config_dir,
                use_tmux=dialog.use_tmux,
                tmux_name=tmux_name,
            )

    def launch_selected_provider(self) -> None:
        self.launch_new(self.new_provider.currentText())

    def add_new_rows_into_group(self, cwd: str, rows: list[dict]) -> None:
        """Register each row (`{"name", "provider", "model", "reasoning_effort"}`)
        into the group at `cwd`, without launching anything - no provider
        CLI, terminal, tmux session, resume, or focus action.

        Skips any name already present in the group - the same merge
        behavior the removed top-level "New session group…" button had
        when pointed at an existing directory. Called from
        ManageGroupDialog's "Add new…" button (LaunchNewGroupSessionsDialog,
        will_launch=False); the group itself must already exist - creating
        one happens either via "Add session to group… → New group…" or
        implicitly the first time a session is filed into a not-yet-existing
        cwd. The row is launched later, the same way any other saved-but-
        not-yet-running row is: per-row Launch, "Launch selected", or the
        row's own double-click/Enter.
        """
        group = self.metadata.setdefault("groups", {}).setdefault(
            cwd, {"cwd": cwd, "rows": []}
        )
        existing_names = {row["name"] for row in group["rows"]}
        for row in rows:
            # Canonicalize before the dedup check, not just inside
            # register_group_row below - two different raw names ("a.b",
            # "a:b") that sanitize to the same tmux identity must collide
            # here, or both mint a row and the second silently overwrites
            # the first's override_key bucket (row447 rework).
            name = sanitize_tmux_session_name(" ".join(str(row["name"]).strip().split()))
            if name in existing_names:
                continue
            provider = row.get("provider", "Claude")
            registered = self.register_group_row(
                cwd,
                name,
                provider,
                row.get("model"),
                row.get("reasoning_effort"),
                row.get("account_config_dir"),
            )
            group["rows"].append(registered)
            existing_names.add(registered["name"])
        write_metadata(self.metadata)
        self.refresh()

    def register_group_row(
        self,
        cwd: str,
        name: str,
        provider: str,
        model_alias: str | None,
        reasoning_effort: str | None = None,
        account_config_dir: str | None = None,
    ) -> dict:
        """Build a saved group row, minting its durable override key.

        `override_key` is a synthetic session key that exists purely so a
        not-yet-launched row still has somewhere to store a model/env/flag
        override, the same way a real session's own `session.key` does - it
        never changes for the life of the row, whether or not it's currently
        matched to a live session (see find_group_member_session, ManageGroupDialog).

        `name` is canonicalized here too (idempotent alongside a caller that
        already did it) - this is the row's one durable minting site, and its
        own docstring said so before any caller actually enforced it.
        """
        name = sanitize_tmux_session_name(" ".join(str(name).strip().split()))
        override_key = f"group:{cwd}#{name}"
        if model_alias or reasoning_effort or account_config_dir:
            entry = self.metadata.setdefault("sessions", {}).setdefault(override_key, {})
            if provider == "Claude":
                if model_alias:
                    entry.setdefault("env", {})["ANTHROPIC_MODEL"] = model_alias
                if account_config_dir:
                    entry.setdefault("env", {})["CLAUDE_CONFIG_DIR"] = account_config_dir
            elif provider == "Codex":
                if model_alias:
                    entry["model"] = model_alias
                if reasoning_effort:
                    entry["reasoning_effort"] = reasoning_effort
        return {"name": name, "provider": provider, "override_key": override_key}

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
        provider = row.get("provider", "Claude")
        use_tmux = self.effective_tmux(group.get("tmux"))
        # No tmux-alive short-circuit here (task-2142): a live-but-detached row (no window open)
        # reaches this function through _focus_or_resume_session precisely because window_titled()
        # found nothing, and an early "already_running" return used to hand back a status dict
        # with no terminal ever opened - a silent no-op. Falling through to the history
        # check/resume_group_row or the fresh self.launch() below is safe regardless of whether
        # the tmux session is already alive: both route through tmux_group_launch_command, whose
        # `has-session -t "=name" || new-session ...; attach -t "=name"` script attaches to an
        # existing session instead of erroring, never spawning a duplicate.

        # A pending marker only describes the process that was just launched.
        # If its tmux row is gone, it is stale and must not mask the row's
        # saved history or cause another orphan transcript on this click.
        if provider == "Codex" and row.pop("codex_pending_since", None) is not None:
            write_metadata(self.metadata)

        # A saved row is a persistent conversation. Reopening it after a
        # reboot must resume its history; a row that has never run (added
        # via "Add new…", never yet launched) has none, and history is
        # exactly what decides that below - not a separate immediate-launch
        # code path. group_row_candidates(), not a same-provider-only claude_sessions()/
        # codex_sessions() call: a row relinked to a different provider than
        # it was saved under has no history under its OWN provider at all,
        # which used to fall through to "no history" and start a duplicate
        # fresh conversation instead of resuming the real one.
        candidates = group_row_candidates(self.metadata, self.settings())
        history = find_group_member_session(
            row, cwd, candidates,
            linked_session_keys=all_linked_member_keys(self.metadata),
        )
        if history is not None:
            return self.resume_group_row(cwd, name)
        # No session_is_tracked_alive gate here: that "Running" read is only
        # ever as good as PID tracking (untrustworthy for anything not
        # launched directly through Session Hub - a tmux group member's own
        # process death between refreshes reads as "Running" for as long as
        # any sibling in the same cwd stays alive; see
        # adopt_untracked_sessions). A regular (non-group) session's own
        # "Resume in new terminal"/double-click has never gated on this
        # either - always trust the click.
        strip_env = (
            ["CLAUDE_CODE_CHILD_SESSION"]
            if provider == "Claude" and row.get("transcripts", True)
            else None
        )
        if provider == "Codex":
            # Only a row with no history reaches here. Mark this one fresh
            # process until its exact tmux rollout is discovered.
            row.pop("session_key", None)
            row["codex_pending_since"] = int(time.time() * 1000)
            write_metadata(self.metadata)
        self.launch(
            provider,
            None,
            cwd,
            model=self.effective_model(row["override_key"], provider) if provider == "Codex" else None,
            reasoning_effort=self.effective_codex_reasoning_effort(row["override_key"])
            if provider == "Codex"
            else None,
            session_key=row["override_key"],
            flag_overrides={"--name": row["name"]},
            strip_env=strip_env,
            wait_for_tracking=wait_for_tracking,
            use_tmux=use_tmux,
        )
        return {"status": "launched", "name": name}

    def rename_group_row(self, cwd: str, old: str, new: str) -> dict:
        """Rename a group row everywhere it is named: metadata (row + override
        bucket) and the live tmux session, so the terminal title follows.
        See rename_group_row_in for why there is one name and not two.

        The tmux side is reconciled BEFORE the metadata write commits (see
        reconcile_tmux_rename) - a target that collides with a different
        live tmux session is refused visibly here instead of leaving the row
        renamed in metadata while tmux never agreed (row447 rework).
        """
        sanitized = sanitize_tmux_session_name(" ".join(str(new).strip().split()))
        reconciled = {"tmux_renamed": False, "error": None}
        if sanitized and sanitized != old:
            reconciled = reconcile_tmux_rename(old, sanitized)
            if reconciled["error"]:
                return {"status": "error", "message": reconciled["error"]}
        result = rename_group_row_in(self.metadata, cwd, old, new)
        if result["status"] == "renamed":
            write_metadata(self.metadata)
            result["tmux_renamed"] = reconciled["tmux_renamed"]
            self.refresh()
        return result

    def resume_group_row(self, cwd: str, name: str) -> dict:
        """Resume a saved group row that already has history.

        The launch_group_row counterpart for a row that isn't fresh - shares
        the same tmux opt-in, so double-clicking an idle row in a
        tmux-enabled group relaunches it inside tmux (via --resume) exactly
        like a first-time launch does, instead of always falling back to a
        plain terminal.
        """
        group = self.metadata.get("groups", {}).get(cwd)
        if not group:
            return {"status": "error", "message": f"No session group for {cwd}"}
        row = next((r for r in group.get("rows", []) if r["name"] == name), None)
        if not row:
            return {"status": "error", "message": f"No row named {name!r} in this group"}
        # group_row_candidates(), not a same-provider-only claude_sessions()/
        # codex_sessions() call: an unlinked, single-provider pool matches
        # the row's stored (possibly stale) native key literally instead of
        # a link's current target, which is the "resume opens an older
        # linked rollout" bug this row exists to fix.
        candidates = group_row_candidates(self.metadata, self.settings())
        live = find_group_member_session(
            row, cwd, candidates,
            linked_session_keys=all_linked_member_keys(self.metadata),
        )
        if not live:
            return {"status": "error", "message": f"{name!r} has no history to resume"}
        # live.provider, not row.get("provider"): a cross-provider link
        # (continue_with_other_agent_for) makes the row's saved provider
        # stale the moment it's continued elsewhere - launching under the
        # row's old provider would run the wrong CLI against live's
        # session_id entirely.
        provider = live.provider
        # No session_is_tracked_alive gate: see the matching comment in
        # launch_group_row.
        self.launch(
            provider,
            live.session_id,
            cwd,
            live.source_cwd,
            model=self.effective_model(row["override_key"], provider)
            if provider == "Codex"
            else None,
            reasoning_effort=self.effective_codex_reasoning_effort(row["override_key"])
            if provider == "Codex"
            else None,
            session_key=row["override_key"],
            use_tmux=self.effective_tmux(group.get("tmux")),
            tmux_name=row["name"],
        )
        return {"status": "resumed", "name": name}

    def add_session_to_group(self) -> None:
        session = self.selected()
        if not session:
            return
        self.add_session_to_group_for(session)

    def add_session_to_group_for(self, session: Session) -> None:
        """File the already-running `session` into a saved group - no launch.

        Picks a target group (any of them, not just one matching the
        session's own cwd - see MoveToGroupDialog), or creates a brand new
        one named on the spot (keyed by the session's own cwd - a group's
        directory is its metadata["groups"] key, see change_group_directory),
        then hands off to file_session_into_group for the actual bookkeeping.
        Either way nothing gets launched.
        """
        groups = self.metadata.setdefault("groups", {})
        dialog = MoveToGroupDialog(groups, session.cwd, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.new_group_name is not None:
            cwd = session.cwd
            if cwd in groups:
                QMessageBox.information(
                    self,
                    "Group already exists",
                    f"A group already exists at “{cwd}” - adding "
                    "the session there instead of creating a new one.",
                )
            else:
                groups[cwd] = {
                    "cwd": cwd,
                    "rows": [],
                    "display_name": dialog.new_group_name,
                }
        elif dialog.cwd:
            cwd = dialog.cwd
        else:
            return
        self.file_session_into_group(session, cwd)

    def file_session_into_group(self, session: Session, cwd: str) -> None:
        """Add a row for the already-running `session` to the group at `cwd`.

        Keys the row by the session's own native id, so
        find_group_member_session recognizes it as that row's live session
        on the next refresh with no new process started. If `cwd` differs
        from the session's own, a cwd override makes the two agree (the same
        mechanism change_directory_for uses) - resuming afterward still opens
        at the session's real location and then /cd's into the group's
        directory, same as any other cwd-overridden session.

        Existing env/flags/name overrides live under the session's native
        key; group members are looked up by their row's override_key instead
        (see matched_sessions), so they're copied forward here - otherwise a
        moved session's model/flags would silently stop applying, the same
        bug remove_row used to have in reverse.
        """
        group = self.metadata["groups"][cwd]
        existing_names = {row["name"] for row in group.get("rows", [])}
        base_name = session.title.strip() or session.session_id[:8]
        name = base_name
        suffix = 2
        while name in existing_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        override_key = f"group:{cwd}#{name}"
        overrides = self.metadata.setdefault("sessions", {})
        native_overrides = overrides.get(session.native_key, {})
        # A fresh dict, not overrides.setdefault(override_key, {}): that name
        # can collide with a stale bucket orphaned by an earlier move/retry
        # (a group row that was since renamed or removed), and reusing it
        # would silently bleed that unrelated session's old name/env/flags
        # into this one instead of the session actually being moved here.
        row_overrides = {}
        for field in ("env", "flags", "name", "model"):
            if field in native_overrides:
                row_overrides[field] = native_overrides[field]
        overrides[override_key] = row_overrides
        if session.cwd != cwd:
            overrides.setdefault(session.native_key, {})["cwd"] = cwd
        group["rows"].append(
            {
                "name": name,
                "provider": session.provider,
                "override_key": override_key,
                "session_key": session.native_key,
            }
        )
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
                (
                    "Group launch options…",
                    lambda: self.edit_group_launch_options(target.cwd),
                ),
                ("Rename group", self.rename_group),
                ("Change directory", self.change_group_directory),
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
                if label != "Continue with other agent"
            ]
        return actions

    def context_menu(self, point) -> None:
        item = self.table.itemAt(point)
        if item is None:
            return
        self.table.setCurrentCell(item.row(), item.column())
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


def sessions_json_cli() -> int:
    """Headless `--sessions-json`: everything the TUI needs, in one call.

    Mirrors the GUI main table exactly (discover_sessions - standalone
    sessions plus one collapsed summary row per group), plus, for every
    saved group, its member rows with the same group_row_status the GUI's
    ManageGroupDialog now shows. Never constructs a QApplication, same as
    diagnostic().
    """
    metadata = read_metadata()
    sessions = discover_sessions(metadata)
    settings = metadata.get("settings", {})
    live: list[Session] = []
    if settings.get("enable_codex", True):
        live += codex_sessions()
    if settings.get("enable_claude", True):
        live += claude_sessions()
    if settings.get("enable_antigravity", True):
        live += antigravity_sessions()

    # One tmux snapshot for the whole CLI invocation - shared by every group
    # row and every session below instead of a `tmux has-session` per call.
    live_names = tmux_live_session_names()

    claimed: set[str] = set()
    groups = {}
    for cwd, group in metadata.get("groups", {}).items():
        explicit = group.get("tmux")
        tmux_enabled = explicit if explicit is not None else settings.get("launch_in_tmux", False)
        rows_out = []
        for row in group.get("rows", []):
            match = find_group_member_session(row, cwd, live, frozenset(claimed))
            if match:
                claimed.add(match.native_key)
            activity_state, activity_detail = (
                session_activity(
                    match, tmux_enabled=tmux_enabled, tmux_name=row["name"], live_names=live_names
                )
                if match else ("unknown", "")
            )
            rows_out.append(
                {
                    "name": row["name"],
                    "provider": row.get("provider", "Claude"),
                    # Liveness (process/tmux) - separate fact from activity below.
                    "status": group_row_status(row, match, tmux_enabled, live_names),
                    "activity": activity_state,
                    "activity_label": activity_label(activity_state)[0],
                    "activity_detail": activity_detail,
                }
            )
        groups[cwd] = {
            "display_name": group.get("display_name") or Path(cwd).name or cwd,
            "tmux": tmux_enabled,
            "rows": rows_out,
        }

    session_overrides = metadata.get("sessions", {}) or {}

    def session_out(item: Session) -> dict:
        is_group = item.session_id.startswith("group:")
        tmux_enabled, tmux_name, status = (
            (False, None, None)
            if is_group
            else standalone_tmux_status(
                item, session_overrides.get(item.key, {}), settings, live_names
            )
        )
        activity_state, activity_detail = session_activity(
            item, tmux_enabled=tmux_enabled, tmux_name=tmux_name, live_names=live_names
        )
        return {
            "provider": item.provider,
            "key": item.key,
            "title": item.title,
            "cwd": item.cwd,
            "session_id": item.session_id,
            "is_group": is_group,
            "updated_ms": item.updated_ms,
            "tmux": tmux_enabled,
            "tmux_name": tmux_name,
            # Liveness (process/tmux) - separate fact from activity below.
            "status": status,
            "activity": activity_state,
            "activity_label": activity_label(activity_state)[0],
            "activity_detail": activity_detail,
        }

    print(
        json.dumps(
            {
                "sessions": [session_out(item) for item in sessions],
                "groups": groups,
            },
            indent=2,
        )
    )
    return 0


def usage_json_cli() -> int:
    """Headless `--usage-json`: the same per-provider usage the GUI's usage
    panel shows, fetched with the same three reader functions
    (UsageWorker.run's providers), run concurrently so the whole call takes
    as long as the slowest single provider, not the sum of all three.
    """
    metadata = read_metadata()
    settings = metadata.get("settings", {})
    readers = {
        "Codex": read_codex_usage,
        "Claude": read_claude_usage,
        "Antigravity": read_antigravity_usage,
    }
    enabled = {
        provider: reader
        for provider, reader in readers.items()
        if settings.get(f"enable_{provider.lower()}", True)
    }
    fetched: dict[str, tuple[list, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(enabled) or 1) as pool:
        futures = {pool.submit(reader): provider for provider, reader in enabled.items()}
        for future in concurrent.futures.as_completed(futures):
            provider = futures[future]
            try:
                fetched[provider] = (future.result(), "")
            except Exception as error:
                fetched[provider] = ([], str(error))

    result = {}
    for provider, (windows, error) in fetched.items():
        if error:
            result[provider] = {"error": error}
            continue
        if windows and isinstance(windows[0], UsageActivity):
            result[provider] = {
                "activity": [
                    {"label": item.label, "requests": item.requests, "sessions": item.sessions}
                    for item in windows
                ]
            }
            continue
        banked = next(
            (w for w in windows if w is not None and w.count is not None), None
        )
        result[provider] = {
            "banked": banked.count if banked else None,
            "windows": [
                {
                    "name": w.name,
                    "used_percent": w.used_percent,
                    "resets": w.resets,
                    "pace": usage_pace_text(w),
                }
                for w in windows
                if w is not None and w.count is None
            ],
        }
    print(json.dumps(result, indent=2))
    return 0


def stop_group_row_cli(argv: list[str]) -> int:
    """Headless `--stop-group-row <cwd> <name>`, the TUI's Stop action.

    Kills the row's tmux session outright (stop_tmux_session already treats
    "no such session" as success - the end state, "not running", either way
    already holds).
    """
    try:
        index = argv.index("--stop-group-row")
        cwd, name = argv[index + 1], argv[index + 2]
    except (ValueError, IndexError):
        print(json.dumps({
            "status": "error",
            "message": "usage: session_hub.py --stop-group-row <cwd> <name>",
        }))
        return 1
    metadata = read_metadata()
    group = metadata.get("groups", {}).get(cwd)
    row = next((r for r in group.get("rows", []) if r["name"] == name), None) if group else None
    if not row:
        print(json.dumps({"status": "error", "message": f"No row named {name!r} in group {cwd!r}."}))
        return 1
    stop_tmux_session(name)
    print(json.dumps({"status": "ok"}))
    return 0


def stop_session_cli(argv: list[str]) -> int:
    """Headless `--stop-session <key>`, the TUI/GUI Stop action for a
    tmux-launched independent (non-group) session.

    Re-derives the tmux name the same way standalone_tmux_status does,
    rather than trusting a caller-supplied name outright, so this can only
    ever stop a tmux session session-hub itself would consider "this
    session's" - not an arbitrary tmux session name.
    """
    try:
        index = argv.index("--stop-session")
        key = argv[index + 1]
    except (ValueError, IndexError):
        print(json.dumps({"status": "error", "message": "usage: session_hub.py --stop-session <key>"}))
        return 1
    metadata = read_metadata()
    session = next((s for s in discover_sessions(metadata) if s.key == key), None)
    if not session or session.session_id.startswith("group:"):
        print(json.dumps({"status": "error", "message": f"No independent session with key {key!r}."}))
        return 1
    overrides = (metadata.get("sessions") or {}).get(key, {})
    tmux_enabled, name, _status = standalone_tmux_status(session, overrides, metadata.get("settings", {}))
    if not tmux_enabled:
        print(json.dumps({"status": "error", "message": f"{key!r} is not launched in tmux."}))
        return 1
    stop_tmux_session(name)
    print(json.dumps({"status": "ok"}))
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


def resume_session_cli(argv: list[str]) -> int:
    """Headless `--resume-session <name|key|id>`, for an orchestrator's own Bash.

    The single-session counterpart to --launch-group-row: same never-shown
    SessionHub, so the resume goes through the exact tracked path (PID
    capture, launch_env/launch_flags overrides) a GUI double-click uses.
    Blocks briefly (wait_for_tracking) since this process exits right after.
    """
    try:
        index = argv.index("--resume-session")
        wanted = argv[index + 1]
    except (ValueError, IndexError):
        print(json.dumps({
            "status": "error",
            "message": "usage: session_hub.py --resume-session <name|key|session-id>",
        }))
        return 1
    app = QApplication.instance() or QApplication(argv[:1])
    window = SessionHub()
    result = window.resume_session_by_name(wanted, wait_for_tracking=True)
    print(json.dumps(result))
    return 0 if result.get("status") != "error" else 1


def main() -> int:
    if "--diagnose" in sys.argv:
        return diagnostic()
    if "--sessions-json" in sys.argv:
        return sessions_json_cli()
    if "--usage-json" in sys.argv:
        return usage_json_cli()
    if "--stop-group-row" in sys.argv:
        return stop_group_row_cli(sys.argv)
    if "--stop-session" in sys.argv:
        return stop_session_cli(sys.argv)
    if "--launch-group-row" in sys.argv:
        return launch_group_row_cli(sys.argv)
    if "--resume-session" in sys.argv:
        return resume_session_cli(sys.argv)
    if "--hook-notify" in sys.argv:
        return hook_notify_cli()
    if "--hook-notify-codex" in sys.argv:
        return hook_notify_codex_cli(sys.argv)
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
