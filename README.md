# Session Hub

A small Linux desktop launcher for local Codex, Claude Code, and Antigravity sessions.

## Features

- Lists local sessions from both agents
- Filters by title, agent, directory, or session ID
- Stores custom names and working-directory overrides without modifying history files
- Shows Codex and Claude 5-hour/weekly usage plus Antigravity's two weekly model pools
- Supports optional per-agent danger-mode launch settings
- Automatically changes Claude's directory after resuming from its original project
- Offers Home, configurable Primary/Secondary project roots, and existing folders
- Moves projects safely between primary and secondary roots in either direction
- Remembers the window size and position
- Restores or permanently deletes trashed sessions from Settings
- Optionally purges deleted sessions after 7, 30, or 90 days
- Continues a task with the other agent through a local context handoff
- Groups linked native transcripts across all three agents into one visible logical session
- Can ask the active agent to prepare a structured full-session handoff summary
- Resumes every session in a separate terminal window
- Running has an embedded terminal (X11 xterm) on the right; click, Enter or double-click a row to
  attach it, `Ctrl+Shift+O` or right-click still opens that row's terminal in its own window
- Starts new Codex, Claude, or Antigravity sessions in a chosen directory
- Moves deleted histories into recoverable application trash

## Requirements

- Python 3
- PyQt6
- GNOME Terminal
- Codex, Claude Code, and/or Antigravity CLI installed
- tmux, [Textual](https://textual.textualize.io/), and the maintained OSS
  [`textual-terminal`](https://github.com/mitosch/textual-terminal) adapter pinned at 0.3.0
  (install with `pip3 install --user --break-system-packages textual-terminal==0.3.0`)

## Install

```bash
./install.sh
```

The launcher appears as **Session Hub** in the desktop application menu. It can
also be started with:

```bash
session-hub
```

Run a non-GUI discovery check with:

```bash
session-hub --diagnose
```

### Phone / SSH TUI

For tmux-launched session groups (see "Launch in tmux" in a group's Manage dialog), a text UI is
available for use over SSH — e.g. from a phone via [Terminus](https://termius.com/) + Tailscale:

```bash
session-hub-tui
```

It mirrors the desktop app: a main session list, a flat "Running" tab (tmux-launched rows only) with
Attach/Launch/Stop, and a "Usage" tab with the same Codex/Claude/Antigravity usage bars as the desktop
panel, stacked in one column. Selecting a running session keeps one maintained OSS
`textual-terminal==0.3.0` widget attached to the exact tmux identity; `Ctrl-L` returns focus to
the list and detaching (`Ctrl-b d`) leaves the menu available.

Session Hub metadata and recoverable trash are stored in
`~/.local/share/session-hub/`.
