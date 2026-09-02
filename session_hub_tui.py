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
import re
import subprocess
import sys
from pathlib import Path

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

SESSION_HUB = Path(__file__).resolve().parent / "session_hub.py"
PHONE_ROW_HEIGHT = 2
RUNNING_CARD_HEIGHT = 3


def compact_running_age(value: object) -> str:
    """Normalize the shared activity age for the two-line Running card."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"now", "just now"}:
        return "0m"
    match = re.fullmatch(r"(\d+)\s*([mhd])(?:\s+ago)?", text)
    return f"{match.group(1)}{match.group(2)}" if match else ""


def elide_running_text(value: object, width: int) -> str:
    """Fit text to a card cell without reserving space for an imaginary column."""
    text = " ".join(str(value or "").split())
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def running_card_lines(row: dict, content_width: int | None = None) -> tuple[str, str]:
    """Return exactly two responsive lines for a selectable Running card."""
    age = compact_running_age(row.get("age", ""))
    name = " ".join(str(row.get("name", "")).split())
    if content_width is None:
        name_width = len(name)
        first_name = elide_running_text(name, name_width)
        first = f"{first_name}{f'  {age}' if age else ''}"
    else:
        reservation = len(age) + (2 if age else 0)
        if age and content_width < reservation:
            first = elide_running_text(age, content_width)
        elif age:
            name_width = content_width - reservation
            first_name = elide_running_text(name, name_width)
            first = f"{first_name:<{name_width}}  {age}"
        else:
            first = elide_running_text(name, content_width)
    second = f"{row.get('provider', '')} · {row.get('display', '')}"
    detail = " ".join(str(row.get("detail", "")).split())
    if detail:
        second += f" · {detail}"
    return first, elide_running_text(second, content_width) if content_width is not None else second


class RunningCard(Static):
    """Two content lines inside a three-cell selectable ListItem target."""

    def __init__(self, row: dict) -> None:
        self.row = row
        super().__init__("\n".join(running_card_lines(row)), classes="running-card")

    def on_mount(self) -> None:
        self._refresh_text()

    def on_resize(self, _event: Resize) -> None:
        self._refresh_text()

    def _refresh_text(self) -> None:
        width = self.content_size.width
        self.update("\n".join(running_card_lines(self.row, width if width else None)))


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
    """Compact scrolling activity list; selecting a live row exits the TUI and hands
    the terminal to a separate `tmux attach`, rather than embedding one."""

    # DataTable claims Enter itself (RowSelected) - see on_data_table_row_selected.
    BINDINGS = [
        ("x", "stop", "Stop"),
        ("r", "refresh", "Refresh"),
        ("ctrl+l", "focus_list", "Focus list"),
    ]

    CSS = """
    # Hug one/few cards with only a small intentional gap; overflow past the cap scrolls.
    #running-list { height: auto; max-height: 12; min-height: 3; border: round $panel; }
    .running-row { height: 3; padding: 0 1; }
    .running-row-sep { border-top: heavy $panel-lighten-2; }
    .running-card { height: 2; content-align: left middle; }
    .running-group { height: 1; padding: 0 1; color: $text-muted; text-style: bold; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._visible_rows: list[dict | None] = []

    def compose(self) -> ComposeResult:
        yield ListView(id="running-list")

    def on_mount(self) -> None:
        self.query_one("#running-list", ListView).focus()

    @staticmethod
    def _identity(row: dict) -> str:
        return str(row.get("key") or f"group:{row.get('cwd', '')}:{row.get('name', '')}")

    @staticmethod
    def _row_text(row: dict, content_width: int | None = None) -> str:
        return "\n".join(running_card_lines(row, content_width))

    def _render_item(self, row: dict, sep: bool = False) -> ListItem:
        classes = "running-row running-row-sep" if sep else "running-row"
        return ListItem(RunningCard(row), classes=classes)

    def apply_sessions(self, data: dict) -> None:
        self.rows = [
            {
                "kind": "group", "cwd": cwd, "name": row["name"], "provider": row["provider"],
                "key": row.get("key") or row.get("session_key"),
                # No saved-name fallback: an undiscovered live target must fail closed in
                # on_list_view_selected, never silently attach to the stale saved name.
                "tmux_name": row.get("tmux_name"),
                "display": group["display_name"], "status_label": row.get("activity_label", ""),
                # Status/activity detail is a separate state signal; the compact row's
                # preview is exclusively the serialized provider-aware assistant text.
                "detail": row.get("assistant_preview", ""), "age": row.get("age", ""),
            }
            for cwd, group in data.get("groups", {}).items()
            for row in group["rows"]
            if row["status"] == "Running"
        ] + [
            {
                "kind": "standalone", "key": s["key"],
                "name": s.get("tmux_name") or s["title"], "provider": s["provider"],
                # No KeyError on a missing discovered name: an absent tmux_name must reach
                # on_list_view_selected's fail-closed check, not crash before it notifies.
                "tmux_name": s.get("tmux_name"), "display": s["title"], "status_label": s.get("activity_label", ""),
                "detail": s.get("assistant_preview", ""), "age": s.get("age", ""),
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
            list_view.append(ListItem(Label(f"{heading} ({len(heading_rows)})", classes="running-group")))
            self._visible_rows.append(None)
            for idx, row in enumerate(heading_rows):
                list_view.append(self._render_item(row, sep=idx > 0))
                self._visible_rows.append(row)
        for heading, rows in groups.items():
            list_view.append(ListItem(Label(f"{heading} ({len(rows)})", classes="running-group")))
            self._visible_rows.append(None)
            for idx, row in enumerate(rows):
                list_view.append(self._render_item(row, sep=idx > 0))
                self._visible_rows.append(row)

    def selected(self) -> dict | None:
        list_view = self.query_one("#running-list", ListView)
        if list_view.index is None or not self._visible_rows:
            return None
        return self._visible_rows[list_view.index]

    def on_list_view_selected(self, _event: ListView.Selected) -> None:
        picked = self.selected()
        if not picked:
            return
        target = picked.get("tmux_name")
        if not target or target.startswith("="):
            self.notify("No exact tmux session to attach to.", severity="error")
            return
        self.app.exit((picked.get("cwd"), target))

    def action_focus_list(self) -> None:
        self.query_one("#running-list", ListView).focus()

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

    @staticmethod
    def _visible_windows(provider: str, windows: list[dict]) -> list[dict]:
        """Omit Spark's redundant model-specific quota rows from the phone TUI."""
        if provider != "Codex":
            return windows
        return [window for window in windows if "spark" not in str(window.get("name", "")).lower()]

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
            for window in self._visible_windows(provider, info.get("windows", [])):
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
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # A non-PTY SSH command (e.g. a phone client's default exec) hands Textual an
        # immediate stdin EOF, which app.run() ends normally with -- not a crash, just a
        # silent no-op that looks like a hang to the user. A forced pseudo-terminal is an
        # invocation property only the client can supply; naming it is the whole fix.
        print(
            "Session Hub TUI needs an interactive terminal (stdin/stdout are not a TTY). "
            "Run it over a forced pseudo-terminal: ssh -tt <host> session-hub-tui "
            "(an existing interactive Termius shell may run session-hub-tui directly).",
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
        # snippet to see the menu again). The exact `=name` target avoids
        # tmux prefix-matching a different live session.
        os.execvp("tmux", ["tmux", "attach", "-t", f"={name}"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
