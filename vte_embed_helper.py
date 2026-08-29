#!/usr/bin/env python3
"""Standalone VTE-based terminal helper for Session Hub's embedded Running-tab terminal
(task-2142 row453 REWORK). A real Vte.Terminal embedded via Gtk.Plug into Session Hub's
container window -- not an xterm approximation -- so font, DPI, theme colors, cursor and the
full interactive TUI (including the Claude/Codex composer/footer) match Session Hub's external
gnome-terminal launch path exactly, because both ultimately resolve the same GNOME
Terminal/desktop settings via `terminal_profile.gnome_terminal_profile_style` -- a small
dependency-free module, deliberately NOT `session_hub.py` itself, which would drag PyQt6 and the
rest of a 9k-line module into a process that only needs a few pure functions (task-2142 row453
REWORK -- orchestrator audit, 2026-08-30).

Session Hub (the Qt process) is the XEMBED "socket" side: it passes its container widget's
native X window id as --socket-id, and after this process is up it explicitly resizes this
window's own X window on every container resize (Gtk.Plug does not track its socket's size on
its own -- see EmbeddedTerminalController.resize_to_container). Vte/GTK handle the resulting
pty resize (SIGWINCH included) as part of ordinary GTK widget resize handling, the same code
path gnome-terminal itself uses.
"""
import argparse
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal_profile import gnome_terminal_profile_style, tmux_exact_target  # noqa: E402


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def apply_style(terminal: "Vte.Terminal", style: dict) -> None:
    font = style.get("font")
    if font:
        terminal.set_font(Pango.FontDescription(font))
    bg = style.get("background")
    fg = style.get("foreground")
    if bg or fg:
        terminal.set_colors(
            _rgba(fg) if fg else None,
            _rgba(bg) if bg else None,
            [],
        )
    cursor_shape = style.get("cursor_shape")
    shapes = {
        "block": Vte.CursorShape.BLOCK,
        "ibeam": Vte.CursorShape.IBEAM,
        "underline": Vte.CursorShape.UNDERLINE,
    }
    if cursor_shape in shapes:
        terminal.set_cursor_shape(shapes[cursor_shape])


def build_plug(socket_id: int, tmux_session: str, profile_uuid: str | None) -> tuple:
    plug = Gtk.Plug.new(socket_id)
    terminal = Vte.Terminal()
    apply_style(terminal, gnome_terminal_profile_style(profile_uuid))
    # spawn_async's raw 12-arg GI binding rejects a bare fire-and-forget call (its
    # child_setup_data_destroy slot is non-nullable in this Vte/PyGObject version despite the
    # GIR annotation) -- spawn_sync's plain fork+exec (blocking only for that step, not for the
    # child's lifetime) is the simple, well-precedented way to do this and needs no callback.
    terminal.spawn_sync(
        Vte.PtyFlags.DEFAULT,
        None,
        ["tmux", "attach-session", "-t", tmux_exact_target(tmux_session)],
        None,
        GLib.SpawnFlags.SEARCH_PATH,
        None,
        None,
    )
    plug.add(terminal)
    plug.connect("destroy", lambda *_a: Gtk.main_quit())
    terminal.connect("child-exited", lambda *_a: Gtk.main_quit())
    return plug, terminal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket-id", type=int, required=True)
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("--profile-uuid", default=None)
    args = parser.parse_args(argv)

    plug, _terminal = build_plug(args.socket_id, args.tmux_session, args.profile_uuid)
    plug.show_all()

    def announce() -> bool:
        window = plug.get_window()
        if window is None:
            return True
        print(f"XID={window.get_xid()}", flush=True)
        return False

    GLib.idle_add(announce)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
