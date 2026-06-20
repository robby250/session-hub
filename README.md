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
- Starts new Codex, Claude, or Antigravity sessions in a chosen directory
- Moves deleted histories into recoverable application trash

## Requirements

- Python 3
- PyQt6
- GNOME Terminal
- Codex, Claude Code, and/or Antigravity CLI installed

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

Session Hub metadata and recoverable trash are stored in
`~/.local/share/session-hub/`.
