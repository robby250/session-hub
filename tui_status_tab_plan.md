> STATUS: decided 2026-08-23 — ACTIVE

# tmux-group status bug fix + Running-sessions tab + phone TUI

## Context

User connects to tmux sessions from their phone via the Terminus app + Tailscale, and wanted a TUI
for session-hub built on the same tmux-launch infrastructure the desktop GUI already uses:
> "we should reuse the PC gui stuff so we don't build the same stuff twice and have drift issues"

Two GUI bugs prompted this. First, `ManageGroupDialog` (the "session group" view) showed
**VAMP-worker4 as "Idle"** even though it was genuinely running — and "Idle" was a confusing third
state on top of that; the user wanted exactly **Running** / **Stopped**. Second, there was no way to
see, at a glance, which sessions across every project were currently running, or to stop one.

## The finding — verified, not hypothesis

`ManageGroupDialog.reload()` (`session_hub.py:3850`, pre-fix) gated "Running" on
`session_is_tracked_alive()` (`session_hub.py:2164`), which only recognizes a process if `PID_DIR` has
a tracking file for it — and both ways a tracking file gets written
(`record_hub_launch`, `adopt_untracked_sessions`/`find_untracked_claude_pids`) are **Claude-only**.
VAMP-worker4 is a **Codex** row (confirmed in `~/.local/share/session-hub/metadata.json`). Its live
process can never get a PID_DIR tracking file, so it could never show anything but "Idle", regardless
of whether it was actually running — confirmed directly: no file under
`~/.local/share/session-hub/pids/*.json` referenced its session id, while `tmux list-sessions` showed
it alive with a matching session name.

**Verified further**: constructing `QApplication` with no `DISPLAY`/`WAYLAND_DISPLAY` crashes
(`qt.qpa.xcb: could not connect to display`) unless `QT_QPA_PLATFORM=offscreen` is set — tested
directly against this environment. This means the TUI, run over plain SSH, can reuse session-hub's
existing headless `--launch-group-row` CLI verb (already used by the VAMPULSE orchestrator's own Bash
tooling) for launching, with zero new launch logic.

## Decisions

| question | answer |
|---|---|
| TUI framework | Textual (`pip3 install --user --break-system-packages textual`, matching how PyQt6 is already installed in this environment) |
| Running/Stopped status source | `tmux has-session -t <name>` — provider-agnostic, standard practice, and the only signal that actually distinguishes Running from Stopped for a Codex/Antigravity tmux row |
| Running/Stopped status scope | tmux-launched sessions only, not plain (non-tmux) launches |
| Stop action | requires a y/n or dialog confirm step, both GUI and TUI (destructive, kills a live process) |
| TUI attach flow | exec-replace into `tmux attach` (the SSH connection becomes the tmux session; detaching ends it) |
| TUI new-row creation | out of scope — GUI-only |
| TUI main list | full parity with the GUI's main table (all discovered sessions, collapsed group rows, searchable) |
| TUI layout | two screens/tabs, mirroring the GUI's two tabs: main list + a separate flat Running list |

## Rejected

- **Reuse an existing tmux session picker** (`sesh`, `tmuxinator`, `tmux-sessionx`, etc.) instead of a
  purpose-built TUI. These are generic tmux pickers with no concept of session-hub's per-project
  groups, its three agent providers, or the launch-a-new-row workflow — they'd only cover "attach to
  something already running," not "see what's stopped and start it," which was the actual ask.
  ([sesh](https://github.com/joshmedeski/sesh), [tmux-sessionx](https://github.com/omerxx/tmux-sessionx))
- **Extend PID-tracking to Codex/Antigravity** (generalize `adopt_untracked_sessions`) instead of
  adding `tmux_session_alive`. Rejected because it's strictly harder — Codex has no persistent
  `--name`-equivalent identity flag on a fresh launch, so exact PID→session matching would stay a
  heuristic guess, where `tmux has-session` is exact and free. `has-session` (not grepping
  `list-sessions`) is the documented standard way to check tmux liveness from a script.
  ([source](https://davidltran.com/blog/check-tmux-session-exists-script/))
- **TUI suspend-and-resume around `tmux attach`** instead of exec-replace. Rejected by the user as
  more moving parts (terminal state save/restore) for a benefit (checking multiple sessions without
  re-running the Terminus snippet) that didn't outweigh the simplicity of exec-replace.

## Changes

- `session_hub.py`: new `tmux_session_alive`, `stop_tmux_session`, `group_row_status` (module-level,
  no Qt); `ManageGroupDialog.reload()` now calls `group_row_status`; new headless CLI verbs
  `--sessions-json` and `--stop-group-row`; new "Running" tab on the main window (`QTabWidget` wrap of
  the existing table + a new flat table with a confirm-gated Stop button).
- `session_hub_tui.py` (new): Textual app, two tabs (`MainPane`, `RunningPane`) plus a drill-down
  `GroupScreen`, talking to `session_hub.py` only via its headless CLI verbs.
- `README.md`, `install.sh`: document/install the new `session-hub-tui` entrypoint.

## Verification

- `python3 -m py_compile session_hub.py session_hub_tui.py` — passed.
- `python3 session_hub.py --sessions-json` against real `metadata.json` (read-only) — confirmed
  VAMP-worker4 now reports `"Running"`, matching `tmux list-sessions` exactly (3/3 rows agreed:
  VAMPULSE-orchestrator, VAMP-worker1, VAMP-worker4).
- Offscreen (`QT_QPA_PLATFORM=offscreen`) construction of the real `SessionHub` GUI window — Running
  tab populated with the same 3 correct rows, All Sessions tab unaffected (7 rows, unchanged).
- Textual headless pilot (`App.run_test()`), real key presses: Main → Enter on a group row drills into
  `GroupScreen` with correct per-row statuses; Escape returns; tab-switch to Running; `x` opens the
  confirm modal; `n` cancels with no state change. Caught and fixed two real bugs this way: `is_group`
  detection was checking the wrong Session field, and `push_screen_wait` needs to run inside a Textual
  worker (`@work`) — both invisible to `py_compile` and only surfaced by actually running the app.
- `tmux_session_alive`/`stop_tmux_session` exercised directly against a disposable tmux session created
  and destroyed for the test (`session-hub-plan-test-dummy`) — not against any real running session.
- **Not verified**: the "confirm → actually stop a real session" path (never sent `y` against a live
  session — VAMP-worker1/4/orchestrator are the user's real work); the actual phone → Tailscale →
  Terminus → SSH path; `install.sh` was syntax-checked (`bash -n`) but not run, since it symlinks into
  `~/.local/bin` and regenerates the desktop entry — the user should run it once themselves
  (`./install.sh`) to pick up `session-hub-tui`.

## Plan archive

Full grilled plan (context, finding, primitives, rejected alternatives, verification plan) as approved
before implementation: `~/.claude/plans/swirling-tumbling-dongarra.md`.
