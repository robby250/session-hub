# Session Hub

A small Linux desktop launcher for local Claude Code and Codex sessions.

## Features

- Lists local sessions from both agents
- Filters by title, agent, directory, or session ID
- Stores custom names and working-directory overrides without modifying history files
- Resumes every session in a separate terminal window
- Starts new Claude or Codex sessions in a chosen directory
- Moves deleted histories into recoverable application trash

## Requirements

- Python 3
- PyQt6
- GNOME Terminal
- Claude Code and/or Codex installed

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
