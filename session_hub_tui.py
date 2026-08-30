#!/usr/bin/env python3
"""Phone/SSH TUI for session-hub, for Terminus + Tailscale + tmux.

Talks to session_hub.py only through its existing headless CLI verbs
(--sessions-json, --launch-group-row, --stop-group-row) - the same code
paths the desktop GUI uses, so there is one core, not two drifting
implementations.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from textual import work
from textual.app import App, ComposeResult
from textual.events import Resize
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    Static,
    TabbedContent,
    TabPane,
)

try:
    from textual_terminal import Terminal as _OssTerminal
except ImportError:  # startup reports the pinned install command; tests can inject a fake adapter
    _OssTerminal = None

SESSION_HUB = Path(__file__).resolve().parent / "session_hub.py"
PHONE_ROW_HEIGHT = 2
TEXTUAL_TERMINAL_INSTALL = "pip3 install --user --break-system-packages textual-terminal==0.3.0"


def run_cli(args: list[str], *, offscreen: bool = False) -> dict:
    env = dict(os.environ)
    if offscreen:
        env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, str(SESSION_HUB), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return {"status": "error", "message": result.stderr.strip() or "no output"}


def sessions_json() -> dict:
    return run_cli(["--sessions-json"])


def usage_json() -> dict:
    return run_cli(["--usage-json"])


def launch_group_row(cwd: str, name: str) -> dict:
    # Constructing QApplication over plain SSH (no DISPLAY) crashes unless
    # QT_QPA_PLATFORM=offscreen - verified directly against this environment.
    return run_cli(["--launch-group-row", cwd, name], offscreen=True)


def stop_group_row(cwd: str, name: str) -> dict:
    return run_cli(["--stop-group-row", cwd, name])


def stop_session(key: str) -> dict:
    return run_cli(["--stop-session", key])


def resume_session(wanted: str) -> dict:
    # Same offscreen-QApplication path as launch_group_row - --resume-session
    # is the standalone counterpart to --launch-group-row.
    return run_cli(["--resume-session", wanted], offscreen=True)


class TerminalAdapter(Protocol):
    """Small lifecycle boundary between RunningPane and the terminal emulator."""

    identity: str
    widget: object

    def start(self) -> None: ...
    def resize(self, width: int, height: int) -> None: ...
    def close(self) -> None: ...


def tmux_attach_argv(name: str) -> list[str]:
    """Build the exact argv passed to textual-terminal's shlex parser.

    textual-terminal 0.3.0 accepts a command string but immediately shlex-splits it and
    calls execvpe; shlex.join therefore preserves argv boundaries without a shell. The child
    environment is rebuilt by the OSS adapter (so inherited TMUX is absent), and the exact
    `=name` target avoids tmux prefix matching.
    """
    tmux = shutil.which("tmux")
    if not tmux:
        raise RuntimeError("tmux is required for the Running terminal")
    if not name or name.startswith("="):
        raise ValueError("empty or malformed tmux session name")
    return [tmux, "attach", "-t", f"={name}"]


class OssTmuxTerminalAdapter:
    """One retained textual-terminal widget attached to exactly one live tmux session."""

    def __init__(self, name: str, terminal_factory=None) -> None:
        factory = terminal_factory or _OssTerminal
        if factory is None:
            raise RuntimeError(f"Install the terminal adapter first: {TEXTUAL_TERMINAL_INSTALL}")
        self.identity = name
        self.argv = tmux_attach_argv(name)
        # textual-terminal parses this with shlex.split then execvpe (no shell), preserving the
        # exact argv and rebuilding an environment without the parent's TMUX marker.
        self.widget = factory(command=shlex.join(self.argv), id="running-terminal")

    def start(self) -> None:
        self.widget.start()

    def resize(self, width: int, height: int) -> None:
        # textual-terminal forwards resize events to its PTY; keep this call behind
        # the adapter so the pane never reaches into the emulator implementation.
        resize = getattr(self.widget, "resize", None)
        if resize is not None:
            resize(width, height)

    def close(self) -> None:
        self.widget.stop()


class ConfirmScreen(ModalScreen[bool]):
    """Minimal y/n confirm modal - stop is destructive, per the grilled decision."""

    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "Cancel")]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box { width: 50; height: auto; border: thick $error; padding: 1 2; }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Stop (y)", id="yes", variant="error")
                yield Button("Cancel (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class GroupScreen(Screen):
    """Drill-down into one group's rows - mirrors the GUI's ManageGroupDialog."""

    # DataTable itself already binds Enter (fires RowSelected) - a BINDINGS
    # entry for "enter" here would never be reached, so activate hooks
    # on_data_table_row_selected instead.
    BINDINGS = [
        ("x", "stop", "Stop"),
        ("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, cwd: str, display_name: str, rows: list[dict]) -> None:
        super().__init__()
        self.cwd = cwd
        self.display_name = display_name
        self.rows = rows

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label(f"{self.display_name}  —  {self.cwd}")
        yield DataTable(id="rows")
        yield Footer()

    def on_mount(self) -> None:
        self.render_rows()
        self.query_one("#rows", DataTable).focus()

    def render_rows(self) -> None:
        table = self.query_one("#rows", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Provider", "Status")
        table.cursor_type = "row"
        for row in self.rows:
            table.add_row(
                row["name"], row["provider"], row["status"], height=PHONE_ROW_HEIGHT
            )

    def selected_row(self) -> dict | None:
        table = self.query_one("#rows", DataTable)
        if table.cursor_row is None or not self.rows:
            return None
        return self.rows[table.cursor_row]

    async def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        row = self.selected_row()
        if not row:
            return
        if row["status"] == "Running":
            self.app.exit((self.cwd, row["name"]))
            return
        self.notify(f"Launching {row['name']}…")
        result = await asyncio.to_thread(launch_group_row, self.cwd, row["name"])
        if result.get("status") == "error":
            self.notify(result.get("message", "Launch failed"), severity="error")
            return
        for _ in range(10):
            await asyncio.sleep(0.5)
            data = await asyncio.to_thread(sessions_json)
            fresh_rows = data.get("groups", {}).get(self.cwd, {}).get("rows", [])
            fresh = next((r for r in fresh_rows if r["name"] == row["name"]), None)
            if fresh and fresh["status"] == "Running":
                self.app.exit((self.cwd, row["name"]))
                return
        self.notify(f"{row['name']} did not come up in time", severity="warning")

    @work
    async def action_stop(self) -> None:
        row = self.selected_row()
        if not row:
            return
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Stop {row['name']!r}?"))
        if not confirmed:
            return
        await asyncio.to_thread(stop_group_row, self.cwd, row["name"])
        row["status"] = "Stopped"
        self.render_rows()


class MainPane(Vertical):
    """All Sessions - mirrors the GUI's main table, full parity, searchable."""

    BINDINGS = [
        ("/", "focus_search", "Search"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict = {}
        self.filtered: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Filter by name, provider, directory, or ID…", id="search")
        yield DataTable(id="main")

    def on_mount(self) -> None:
        # Shell paints immediately with an empty, correctly-columned table;
        # SessionHubTUI.fetch_sessions() publishes the one shared generation
        # to this pane (and RunningPane) once the async fetch completes -
        # see apply_sessions().
        table = self.query_one("#main", DataTable)
        table.add_columns("Provider", "Name", "Working directory", "Session ID")
        table.cursor_type = "row"

    def apply_sessions(self, data: dict) -> None:
        self.data = data
        self.apply_filter(self.query_one("#search", Input).value)

    def apply_filter(self, query: str) -> None:
        query = query.strip().lower()
        sessions = self.data.get("sessions", [])
        if query:
            self.filtered = [
                s for s in sessions if query in " ".join(str(v) for v in s.values()).lower()
            ]
        else:
            self.filtered = list(sessions)
        table = self.query_one("#main", DataTable)
        table.clear(columns=True)
        table.add_columns("Provider", "Name", "Working directory", "Session ID")
        table.cursor_type = "row"
        for session in self.filtered:
            table.add_row(
                "Group" if session["is_group"] else session["provider"],
                session["title"],
                session["cwd"],
                session["session_id"],
                height=PHONE_ROW_HEIGHT,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.apply_filter(event.value)

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self.app.fetch_sessions()

    async def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        table = self.query_one("#main", DataTable)
        if table.cursor_row is None or not self.filtered:
            return
        session = self.filtered[table.cursor_row]
        if not session["is_group"]:
            await self.handle_standalone(session)
            return
        cwd = session["cwd"]
        group = self.data.get("groups", {}).get(cwd)
        if not group:
            self.notify("No group metadata for this session.", severity="error")
            return
        self.app.push_screen(GroupScreen(cwd, group["display_name"], group["rows"]))

    async def handle_standalone(self, session: dict) -> None:
        if not session["tmux"]:
            self.notify(f"{session['title']}: not launched in tmux (use the desktop GUI).")
            return
        if session["status"] == "Running":
            self.app.exit((None, session["tmux_name"]))
            return
        self.notify(f"Launching {session['title']}…")
        result = await asyncio.to_thread(resume_session, session["key"])
        if result.get("status") == "error":
            self.notify(result.get("message", "Launch failed"), severity="error")
            return
        for _ in range(10):
            await asyncio.sleep(0.5)
            data = await asyncio.to_thread(sessions_json)
            fresh = next((s for s in data.get("sessions", []) if s["key"] == session["key"]), None)
            if fresh and fresh["status"] == "Running":
                self.app.exit((None, fresh["tmux_name"]))
                return
        self.notify(f"{session['title']} did not come up in time", severity="warning")


class RunningPane(Vertical):
    """Compact scrolling activity list above one retained exact-identity terminal."""

    # DataTable claims Enter itself (RowSelected) - see on_data_table_row_selected.
    BINDINGS = [
        ("x", "stop", "Stop"),
        ("r", "refresh", "Refresh"),
        ("ctrl+l", "focus_list", "Focus list"),
    ]

    CSS = """
    # Keep the activity list in the upper third of a phone viewport; overflow scrolls here.
    # The terminal owns the remaining height and never gets pushed below the screen.
    #running-list { height: 12; min-height: 5; border: round $panel; }
    #terminal-host { height: 1fr; min-height: 4; border: round $panel; }
    #terminal-empty { height: 1fr; content-align: center middle; color: $text-muted; }
    .running-row { height: 2; padding: 0 1; }
    .running-group { height: 1; padding: 0 1; color: $text-muted; text-style: bold; }
    """

    def __init__(self, adapter_factory=OssTmuxTerminalAdapter) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._visible_rows: list[dict | None] = []
        self.selected_key: str | None = None
        self.adapter_factory = adapter_factory
        self.adapter: TerminalAdapter | None = None

    def compose(self) -> ComposeResult:
        yield ListView(id="running-list")
        with Vertical(id="terminal-host"):
            yield Static("Select a running session", id="terminal-empty")

    def on_mount(self) -> None:
        self.query_one("#running-list", ListView).focus()

    @staticmethod
    def _identity(row: dict) -> str:
        return str(row.get("key") or f"group:{row.get('cwd', '')}:{row.get('name', '')}")

    @staticmethod
    def _row_text(row: dict) -> str:
        detail = " ".join(str(row.get("detail", "")).split())
        if len(detail) > 60:
            detail = detail[:59] + "…"
        age = str(row.get("age", ""))
        first = row["name"] + (f"  {age}" if age else "")
        second = f"{row['provider']} · {row['display']}"
        return f"{first}\n{second}" + (f"\n{detail}" if detail else "")

    def apply_sessions(self, data: dict) -> None:
        self.rows = [
            {
                "kind": "group", "cwd": cwd, "name": row["name"], "provider": row["provider"],
                "key": row.get("key") or row.get("session_key"),
                "tmux_name": row.get("tmux_name") or row["name"],
                "display": group["display_name"], "status_label": row.get("activity_label", ""),
                "detail": row.get("activity_detail", ""), "age": row.get("age", ""),
            }
            for cwd, group in data.get("groups", {}).items()
            for row in group["rows"]
            if row["status"] == "Running"
        ] + [
            {
                "kind": "standalone", "key": s["key"], "name": s["tmux_name"], "provider": s["provider"],
                "tmux_name": s["tmux_name"], "display": s["title"], "status_label": s.get("activity_label", ""),
                "detail": s.get("activity_detail", ""), "age": s.get("age", ""),
            }
            for s in data.get("sessions", [])
            if not s["is_group"] and s["status"] == "Running"
        ]
        list_view = self.query_one("#running-list", ListView)
        list_view.clear()
        self._visible_rows = []
        groups: dict[str, list[dict]] = {}
        for row in self.rows:
            groups.setdefault(row.get("status_label") or "Unknown", []).append(row)
        order = ("Working", "Needs input", "Done", "Idle", "Unknown")
        for heading in order:
            heading_rows = groups.pop(heading, [])
            if not heading_rows:
                continue
            list_view.append(ListItem(Label(heading, classes="running-group")))
            self._visible_rows.append(None)
            for row in heading_rows:
                item = ListItem(Label(self._row_text(row), classes="running-row"))
                item.name = self._identity(row)
                list_view.append(item)
                self._visible_rows.append(row)
        for heading, rows in groups.items():
            list_view.append(ListItem(Label(heading, classes="running-group")))
            self._visible_rows.append(None)
            for row in rows:
                item = ListItem(Label(self._row_text(row), classes="running-row"))
                item.name = self._identity(row)
                list_view.append(item)
                self._visible_rows.append(row)
        # Activity labels remain in each compact row; preserve a stable order
        # while avoiding a second widget hierarchy that could steal height.
        # (The status is deliberately data, not inferred from transcript text.)
        keys = {self._identity(row) for row in self.rows}
        if self.selected_key not in keys:
            self.selected_key = None
            self._close_adapter()
        elif self.selected_key:
            list_view.index = next(i for i, row in enumerate(self._visible_rows) if row and self._identity(row) == self.selected_key)

    def _close_adapter(self) -> None:
        if self.adapter is not None:
            self.adapter.close()
            self.adapter = None
        host = self.query_one("#terminal-host", Vertical)
        host.remove_children()
        host.mount(Static("Select a running session", id="terminal-empty"))

    def selected(self) -> dict | None:
        list_view = self.query_one("#running-list", ListView)
        if list_view.index is None or not self._visible_rows:
            return None
        return self._visible_rows[list_view.index]

    async def on_list_view_selected(self, _event: ListView.Selected) -> None:
        picked = self.selected()
        if not picked:
            return
        await self._switch_terminal(picked)

    async def _switch_terminal(self, picked: dict) -> None:
        identity = self._identity(picked)
        target = picked.get("tmux_name") or picked["name"]
        if self.selected_key == identity and self.adapter is not None:
            return
        self._close_adapter()
        self.selected_key = identity
        try:
            adapter = self.adapter_factory(target)
        except (RuntimeError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        host = self.query_one("#terminal-host", Vertical)
        await host.mount(adapter.widget)
        self.adapter = adapter
        adapter.start()
        adapter.widget.focus()

    def action_focus_list(self) -> None:
        self.query_one("#running-list", ListView).focus()

    def on_resize(self, event: Resize) -> None:
        if self.adapter is not None:
            self.adapter.resize(event.size.width, event.size.height)

    @work
    async def action_stop(self) -> None:
        picked = self.selected()
        if not picked:
            return
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Stop {picked['name']!r}?"))
        if not confirmed:
            return
        if picked["kind"] == "group":
            await asyncio.to_thread(stop_group_row, picked["cwd"], picked["name"])
        else:
            await asyncio.to_thread(stop_session, picked["key"])
        if self.selected_key == self._identity(picked):
            self._close_adapter()
            self.selected_key = None
        self.app.fetch_sessions()

    def action_refresh(self) -> None:
        self.app.fetch_sessions()


class UsagePane(VerticalScroll):
    """Per-provider usage bars - same data and layout logic as the GUI's usage
    panel (SessionHub.usage_loaded), stacked in one column instead of the
    GUI's side-by-side columns: there's plenty of vertical room on a phone
    and none to spare horizontally.
    """

    BINDINGS = [("r", "refresh", "Refresh")]
    PROVIDERS = ("Codex", "Claude", "Antigravity")

    def __init__(self) -> None:
        super().__init__()
        self.data: dict = {}
        self._loaded = False

    def compose(self) -> ComposeResult:
        yield Label("Usage loads when this tab is opened.", id="usage-status")

    def ensure_loaded(self) -> None:
        """Usage does no work (no subprocess call) until this tab is first
        opened - `SessionHubTUI.on_tabbed_content_tab_activated` calls this,
        not `on_mount`, since TabbedContent mounts every TabPane's children
        up front regardless of which tab is visible."""
        if self._loaded:
            return
        self._loaded = True
        self.refresh_data()

    def action_refresh(self) -> None:
        self._loaded = True
        self.refresh_data()

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        await self.remove_children()
        await self.mount(Label("Loading usage…", id="usage-status"))
        self.data = await asyncio.to_thread(usage_json)
        await self.rebuild()

    async def rebuild(self) -> None:
        await self.remove_children()
        mounted_any = False
        for provider in self.PROVIDERS:
            info = self.data.get(provider)
            if not info:
                continue
            mounted_any = True
            banked = info.get("banked")
            title = f"[b]{provider} usage[/b]"
            if banked:
                title += f" · {banked} banked reset{'' if banked == 1 else 's'} available"
            await self.mount(Label(title, classes="usage-header"))
            if info.get("error"):
                await self.mount(Label(f"Unavailable: {info['error']}", classes="usage-detail"))
                continue
            for window in info.get("windows", []):
                remaining = 100 - window["used_percent"]
                await self.mount(Label(f"{window['name']} — {remaining}% left ({window['used_percent']}% used)"))
                bar = ProgressBar(total=100, show_eta=False)
                await self.mount(bar)
                bar.progress = remaining
                detail = window["resets"]
                if window.get("pace"):
                    detail += f"\n{window['pace']}"
                await self.mount(Label(detail, classes="usage-detail"))
        if not mounted_any:
            await self.mount(Label("No usage data (all providers disabled in Settings?)"))


class SessionHubTUI(App):
    """Three tabs: All Sessions and Running (mirroring the GUI's two tabs),
    plus Usage (mirroring the GUI's usage panel)."""

    CSS = """
    .usage-header { margin-top: 1; }
    .usage-detail { color: $text-muted; margin-bottom: 1; }
    """
    TITLE = "Session Hub"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="main"):
            with TabPane("All Sessions", id="main"):
                yield MainPane()
            with TabPane("Running", id="running"):
                yield RunningPane()
            with TabPane("Usage", id="usage"):
                yield UsagePane()
        yield Footer()

    def on_mount(self) -> None:
        # Shell (Header/tabs/empty tables) is already painted by compose()
        # before this runs; the one shared sessions fetch happens async and
        # publishes atomically to both MainPane and RunningPane.
        self.fetch_sessions()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "usage":
            self.query_one(UsagePane).ensure_loaded()

    @work(exclusive=True)
    async def fetch_sessions(self) -> None:
        data = await asyncio.to_thread(sessions_json)
        if data.get("status") == "error":
            # Failure leaves whatever generation the panes already have
            # (the initial empty shell, or the last good fetch) intact.
            self.notify(data.get("message", "Failed to refresh sessions"), severity="error")
            return
        self.query_one(MainPane).apply_sessions(data)
        self.query_one(RunningPane).apply_sessions(data)


def main() -> int:
    if _OssTerminal is None:
        print(
            f"Session Hub Running terminal requires the maintained OSS adapter; install with: "
            f"{TEXTUAL_TERMINAL_INSTALL}",
            file=sys.stderr,
        )
        return 2
    app = SessionHubTUI()
    result = app.run()
    if isinstance(result, tuple) and len(result) == 2:
        _cwd, name = result
        # Exec-replace: the terminal IS the tmux session from here on
        # (decided over suspend-and-resume - simpler, standard tmux/SSH
        # behavior; detaching ends the SSH connection, re-run the Terminus
        # snippet to see the menu again).
        os.execvp("tmux", ["tmux", "attach", "-t", name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
