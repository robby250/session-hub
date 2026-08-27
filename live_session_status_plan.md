> STATUS: decided 2026-08-27 — ACTIVE

# Live per-session status (Working / Needs input / Done / Idle) in the Running tab

## Context
User wanted a CodeAgentSwarm-style at-a-glance view of parallel Claude Code sessions in the Running
tab's currently-blank space:
> "in the running tab we have some space to put notifications and stuff when one is done or idle or
> needs input or working... look at all that space"

Constraint: must update automatically without ever triggering the existing manual usage-refresh
(`refresh_usage`, only called from `refresh_all` — F5/Refresh button/startup):
> "it should update automatically but without running a full refresh because the refresh i run
> manually when i want to see usage and i don't want that to run automatically"

## The finding — verified
- `refresh_running_tab()` (session_hub.py) was already the cheap, usage-free path — called
  standalone after row actions without ever touching `refresh_usage()`. Safe to put on a timer.
- `refresh_running_tab` already resolves each row's Claude `session_id` via
  `find_group_member_session`/`standalone_tmux_status` — exactly the join key a Claude Code hook
  payload's own `session_id` field needs, no new identity scheme required.
- `CLAUDE_PROJECTS` (session_hub.py) is hardcoded to `~/.claude/projects`, not `CLAUDE_CONFIG_DIR`-
  aware — alt-account sessions were already invisible to `refresh_running_tab` before this change;
  not fixed here, not a regression either.
- Official docs (code.claude.com/docs/en/hooks): `Notification` and `Stop` hooks both receive
  `session_id`, `cwd`, `transcript_path`, `hook_event_name` on stdin JSON. `Notification` carries a
  `notification_type` with confirmed values `permission_prompt`, `idle_prompt`, `agent_needs_input`,
  `agent_completed` — Claude Code already semantically labels "needs input" vs "finished".
- Same docs: hook entries **merge** across `~/.claude/settings.json` → project
  `.claude/settings.json` → `.claude/settings.local.json`. Project-level files are **not relocated
  by `CLAUDE_CONFIG_DIR`** — writing to a project's own `.claude/settings.local.json` sidesteps the
  multi-account gap above entirely and never touches the user's global Claude config.
- Prior art: AgentsRoom (agentsroom.dev) is a published multi-agent dashboard built on the
  `Notification` hook for exactly this signal — confirms hooks over tmux
  `monitor-activity`/`monitor-bell`, already ruled out earlier in this session as too coarse (fires
  on any pane output, not specifically "waiting for input").
- One research agent's report (via claude-code-guide) was flagged by the harness as
  instruction-shaped and contained claims (an "Agent View", `ListAgents`/cross-session messaging,
  HTTP-type hooks) that don't appear in the official docs fetched directly — not used; likely
  confused Claude Code CLI docs with this session's own orchestration tooling of the same names.

## Decisions

| question | answer |
|---|---|
| Hook install scope | Per-project `.claude/settings.local.json`, not global `~/.claude/settings.json` |
| Install trigger | New Settings toggle "Enable live session status", off by default; merges hooks into a project's `settings.local.json` on first launch there while on |
| Poll interval | `QTimer` every 2s, calling `refresh_running_tab()` only |
| UI treatment | Status column (color-coded) + Last message column (~80 chars, ellipsized, full text in tooltip) on `running_table`, plus a Recent-activity strip below listing the last 20 transitions |
| State set | 4 states: Working / Needs input / Done / Idle |
| Done → Idle owner | Either: double-clicking the row (`reveal_running_row`), or the terminal's window regaining OS focus by any means (checked each 2s poll tick via `xdotool getactivewindow getwindowname`) — user's own wording: "opening the row or focusing the terminal should clear it" |
| Toggle-off cleanup | Best-effort: hook entries matched by their exact `--hook-notify` command string are stripped from every known project's `settings.local.json`; everything else in that file is left alone |
| Visual style | Match the app's existing palette (`#5aa9ff`/`#d9534f`/`#d69e2e`/`#888` — same vocabulary as the usage bars and provider colors) |

## Rejected
- **tmux `monitor-activity`/`monitor-bell`** — fires on any pane output, not specifically "waiting
  for input"; would badge constantly while the agent is still working. (Established earlier this
  session, during the unrelated AskUserQuestion-AFK-timeout diagnosis.)
- **4 states without a distinct Done** (3-state Working/Needs input/Idle) — my own initial
  recommendation, to sidestep the exact multi-writer badge-drift bug shown in the CodeAgentSwarm
  screenshot. User chose 4 states anyway, wanting a distinct "just finished, unseen" signal; drift
  is avoided instead by keeping one JSON file per session as the single source for both the column
  and the activity strip, and one well-defined pair of Done→Idle owners.
- **Time-based decay for Done → Idle** — doesn't actually mean "seen"; rejected in favor of
  action-based clearing (row double-click or real terminal focus).
- **Global `~/.claude/settings.json` for hook install** — one write covers every project, but
  pollutes global Claude config, fires for sessions launched entirely outside session-hub, and
  misses alt-`CLAUDE_CONFIG_DIR` accounts. Project-local `settings.local.json` avoids all three.

## Verification
- `python3 -c "import ast; ast.parse(open('session_hub.py').read())"` — passed.
- **Not run**: `test_session_hub.py`/pytest — standing constraint on this repo (a prior
  patch-scoping bug in the test suite wiped the real `metadata.json` once).
- **Not yet done**: the manual end-to-end pass (enable toggle, launch a group row, trigger a real
  permission prompt, confirm Needs input → Done → Idle transitions, confirm hook removal on
  toggle-off) — only a human running the real GUI against a real tmux session can confirm this;
  code-complete but functionally unverified beyond syntax.
- Unverified mechanism: whether Claude Code's `Notification` hook actually fires for `idle_prompt`
  inside session-hub's specific detached-then-attached tmux launch order. The earlier diagnosis this
  session proved OS-level focus-event delivery works through that same launch order; hook firing is
  a separate, still-unconfirmed mechanism.

## Implementation
All in `session_hub.py`: `STATUS_DIR` constant; `write_session_status`/`read_session_status`/
`all_session_statuses`/`hook_event_to_status`/`hook_notify_cli` (new `--hook-notify` CLI flag);
`install_status_hooks`/`uninstall_status_hooks`; `active_window_title`; Settings toggle
`status_hooks_enabled` + `SessionHub.uninstall_status_hooks_everywhere`; `running_table` grown to 5
columns + `activity_list` (`QListWidget`) in `build_ui`; `refresh_running_tab` rewritten to resolve
and render status; `reveal_running_row`/`stop_selected_running` updated for the 3-tuple row payload;
`QTimer(2000ms)` in `SessionHub.__init__`.

## Codex CLI support (2026-08-27, second pass)

### Context
User: *"aren't there codex hooks too? i have codex CLIs too"* — the above only wired up Claude
(`provider == "Claude"` gate). Explicit go-ahead: *"yeah do a full research+implement pass"*.

### Finding — verified
- Codex session identity already resolves today, same shape as Claude's `session_id`:
  `codex_sessions()` (session_hub.py:1422) reads `CODEX_STATE` (`~/.codex/state_5.sqlite`) table
  `threads(id, title, cwd, updated_at_ms, rollout_path)` — `id` is a real per-thread identifier.
- Codex has two extension mechanisms, not one. `notify` (`config.toml`, single-command,
  **user-level only** — confirmed explicitly not project-overridable, learn.chatgpt.com/docs/
  config-file/config-reference) fires **only** for `agent-turn-complete`
  (codex.danielvaughan.com, jw1.dev — both confirm no other event reaches it, and jw1.dev states
  approval prompts are a separate, non-hookable `tui.notifications` mechanism). Payload confirmed
  **kebab-case** by two independent concrete sources (backgrind.com's quoted JSON example,
  jw1.dev's actual filter script): `type`, `thread-id`, `turn-id`, `cwd`, `client` (optional),
  `input-messages` (optional), `last-assistant-message` (optional).
- A newer lifecycle **hooks engine** (`hooks.json`, event names mirroring Claude Code's) is
  project-scriptable, but every fetch attempting its payload schema (learn.chatgpt.com/docs/hooks,
  doc.jarvisuni.com) returned fields identical to Claude Code's own hook payload
  (`session_id`, `transcript_path`, `hook_event_name`, `permission_mode`, `model`) — the same
  hallucination pattern already flagged once this session with a claude-code-guide subagent
  report. Not trustworthy, not used.
- No existing `notify` key on this machine's `~/.codex/config.toml` (checked directly).

## Decisions
| question | answer |
|---|---|
| Codex state set | Done only — no Working/Needs-input signal exists for Codex. Same Done→Idle clearing as Claude. |
| Install scope | Same Settings toggle; for Codex it writes the global `~/.codex/config.toml` `notify` key (not per-project — `notify` can't be). Tooltip says so plainly. |
| Notify collision | Refuse to overwrite an existing non-Session-Hub `notify` command; warn once via `QMessageBox` instead. |
| TOML editing | `tomllib` (stdlib) to read; a targeted regex line-patch to write the single `notify = [...]` line — no new dependency, rest of the file untouched. |
| Uninstall | Mirrors the Claude path: `uninstall_status_hooks_codex()` removes the `notify` line only if it's still exactly Session Hub's own command. |

## Rejected
- **Codex's lifecycle hooks engine** — closer parity with Claude's 4-state model, but its payload
  schema could not be verified from any trustworthy source; every fetch returned what appears to
  be copy-pasted Claude Code documentation. Revisit if OpenAI publishes a verified schema.
- **Chaining an existing user `notify` command** — considered, rejected as more code for a
  collision that doesn't exist on this machine today; refuse-and-warn is simpler and non-destructive.

## Implementation (Codex pass)
`session_hub.py`: `CODEX_CONFIG` constant; `hook_event_to_status_codex`; `hook_notify_codex_cli`
(new `--hook-notify-codex` CLI flag, reads the payload from the **last argv element**, since Codex
appends it after the configured `notify` command rather than piping stdin like Claude);
`codex_notify_command`/`_read_codex_notify`/`install_status_hooks_codex`/
`uninstall_status_hooks_codex`; `SessionHub.launch()`'s gate extended to cover `provider ==
"Codex"`, warning once (`self._codex_notify_warned`) on a real collision;
`uninstall_status_hooks_everywhere` now also calls `uninstall_status_hooks_codex()`; Settings
tooltip updated to explain the global-vs-per-project distinction. `refresh_running_tab` needed no
change — it already reads status by `session_id` generically regardless of provider.

## Verification (Codex pass)
- `python3 -c "import ast; ast.parse(open('session_hub.py').read())"` — passed.
- **Not run**: pytest (standing repo constraint).
- **Not verified**: a real Codex `agent-turn-complete` notify invocation end-to-end (config write
  confirmed by code path, but no live Codex session was run to trigger it this pass). The argv
  payload shape (last element, kebab-case fields) rests on two independent but still third-party
  sources, not an official OpenAI schema doc — treat as best-effort until seen firing for real.
