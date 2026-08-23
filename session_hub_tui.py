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
import subprocess
import sys
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, TabbedContent, TabPane

SESSION_HUB = Path(__file__).resolve().parent / "session_hub.py"


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


def launch_group_row(cwd: str, name: str) -> dict:
    # Constructing QApplication over plain SSH (no DISPLAY) crashes unless
    # QT_QPA_PLATFORM=offscreen - verified directly against this environment.
    return run_cli(["--launch-group-row", cwd, name], offscreen=True)


def stop_group_row(cwd: str, name: str) -> dict:
    return run_cli(["--stop-group-row", cwd, name])


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
            table.add_row(row["name"], row["provider"], row["status"])

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

    # DataTable claims Enter itself (RowSelected) - see on_data_table_row_selected.
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
        self.refresh_data()

    def refresh_data(self) -> None:
        self.data = sessions_json()
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
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.apply_filter(event.value)

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        self.refresh_data()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        table = self.query_one("#main", DataTable)
        if table.cursor_row is None or not self.filtered:
            return
        session = self.filtered[table.cursor_row]
        if not session["is_group"]:
            self.notify(f"{session['title']}: not tmux-controllable from here (use the desktop GUI).")
            return
        cwd = session["cwd"]
        group = self.data.get("groups", {}).get(cwd)
        if not group:
            self.notify("No group metadata for this session.", severity="error")
            return
        self.app.push_screen(GroupScreen(cwd, group["display_name"], group["rows"]))


class RunningPane(Vertical):
    """Flat list of every currently-running tmux row, across every project."""

    # DataTable claims Enter itself (RowSelected) - see on_data_table_row_selected.
    BINDINGS = [
        ("x", "stop", "Stop"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str, dict]] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="running")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        data = sessions_json()
        self.rows = [
            (cwd, group["display_name"], row)
            for cwd, group in data.get("groups", {}).items()
            for row in group["rows"]
            if row["status"] == "Running"
        ]
        table = self.query_one("#running", DataTable)
        table.clear(columns=True)
        table.add_columns("Project", "Name", "Provider")
        table.cursor_type = "row"
        for cwd, display_name, row in self.rows:
            table.add_row(display_name, row["name"], row["provider"])

    def selected(self) -> tuple[str, str, dict] | None:
        table = self.query_one("#running", DataTable)
        if table.cursor_row is None or not self.rows:
            return None
        return self.rows[table.cursor_row]

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        picked = self.selected()
        if not picked:
            return
        cwd, _display_name, row = picked
        self.app.exit((cwd, row["name"]))

    @work
    async def action_stop(self) -> None:
        picked = self.selected()
        if not picked:
            return
        cwd, _display_name, row = picked
        confirmed = await self.app.push_screen_wait(ConfirmScreen(f"Stop {row['name']!r}?"))
        if not confirmed:
            return
        await asyncio.to_thread(stop_group_row, cwd, row["name"])
        self.refresh_data()

    def action_refresh(self) -> None:
        self.refresh_data()


class SessionHubTUI(App):
    """Two tabs: All Sessions (mirrors the GUI main table) and Running (mirrors the GUI's new tab)."""

    TITLE = "Session Hub"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="main"):
            with TabPane("All Sessions", id="main"):
                yield MainPane()
            with TabPane("Running", id="running"):
                yield RunningPane()
        yield Footer()


def main() -> int:
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
