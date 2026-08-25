> STATUS: decided 2026-08-25 — ACTIVE

# Multi-account Claude Code + Session Hub account picker

## Context

User runs 2 Claude accounts (both 5x/Max) and wants them usable at the same time, picked per
session/group row from Session Hub, with skills/memory/plugins/settings identical across both — no
drift:
> "i want to be able to pick from session hub which session launches with which account and other
> than that i want their memory and skills and plugins and absolutely everything else to be
> identical and not drift so like with symlinks or something"

Referenced two write-ups on `CLAUDE_CONFIG_DIR`-based account isolation (a Medium post, and a gist).

## The finding — verified

- `CLAUDE_CONFIG_DIR` redirects Claude Code's entire config root. This machine's `~/.claude/` mixes
  **already-synced** items (`CLAUDE.md`, `settings.json`, `settings.local.json`, `skills` — symlinks
  into `~/Dropbox/Backups/projects/claude-config/`) with **local-only** items:
  `.credentials.json` (plaintext OAuth token, no keychain on Linux), `stats-cache.json`, `daemon*`,
  `sessions/`, `mcp-needs-auth-cache.json`, `history.jsonl`.
- Session Hub already had the plumbing for arbitrary per-row env vars — `group_env_overrides()`
  (`session_hub.py`) merges global → group → session env into every tmux-launched Claude process,
  and the Model picker (`register_group_row`/`effective_model`) is the exact template used here for
  Account, storing/reading `CLAUDE_CONFIG_DIR` the same way `ANTHROPIC_MODEL` already is.
- **`CLAUDE_CONFIG_DIR` does not relocate `~/.claude.json`** (sibling to `~/.claude/` — project trust
  list, onboarding flags, MCP server list, no credentials). Confirmed via
  [anthropics/claude-code#25998](https://github.com/anthropics/claude-code/issues/25998) and
  [#28808](https://github.com/anthropics/claude-code/issues/28808) — still open/known. A Windows
  write-up on the same problem instead overrode `HOME` entirely per account; not adopted here (see
  Decisions).
- A brand-new (non-group) session has no `session_key` at launch time to hang a stored env override
  on, unlike a group row — Claude's `model` for that case already flows through an explicit
  `--model` CLI flag, but `CLAUDE_CONFIG_DIR` has no CLI-flag equivalent, so `launch()`/`spawn()`
  needed a new `account_config_dir`/`extra_env` parameter to inject it directly for that one case.

## Decisions

| question | answer |
|---|---|
| `.claude.json` isolation | Share it — `CLAUDE_CONFIG_DIR` only, real `$HOME` untouched. No auth data lives in it; collision risk limited to a shared project-trust list and onboarding flags. Rejected: full `HOME` override (isolates `.claude.json` fully, but every subprocess Claude spawns — git, gh, ssh, npm — would need its own dotfiles symlinked into a fake home too, or those tools break inside that account's sessions). |
| Setup-script isolate list | `.credentials.json`, `stats-cache.json`, `daemon*`, `sessions/`, `mcp-needs-auth-cache.json`, `.last-cleanup`, `.last-update-result.json`, `history.jsonl`. Everything else (skills, settings, `CLAUDE.md`, plugins, rules, output-styles, `projects/` incl. memory) symlinked shared. |
| Session Hub UI | Full picker: `account_combo` in `NewSessionDialog`, `AgentModelEffortDialog`, and the per-row table in `LaunchNewGroupSessionsDialog`; `claude_accounts` registry (name → `CLAUDE_CONFIG_DIR`) in Session Hub's own settings, edited via a reused `EnvEditor` widget; stored/read through the same `entry["env"]` pattern as `ANTHROPIC_MODEL`. |
| Account 2 naming | `~/.claude-2`. Account 1 stays the existing `~/.claude`, untouched, no re-auth. |

## What changed

- `setup_claude_account.sh` (new): symlinks every top-level `~/.claude` entry into a target account
  dir except the isolate list, which it creates fresh/empty instead. Idempotent — only fills in
  entries missing from the target. Run once for `~/.claude-2`.
- `session_hub.py`:
  - `DEFAULT_CLAUDE_ACCOUNTS`, `populate_claude_account_combo()` — new, alongside `CLAUDE_MODELS`.
  - `ENV_VAR_SPECS["CLAUDE_CONFIG_DIR"]` — new spec so the raw per-session env editor
    (`SessionLaunchOptionsDialog`, which has no dedicated Claude model/account combo, matching its
    existing asymmetry with the other 3 dialogs) surfaces it with a description.
  - `SessionHub.effective_account()` — new, mirrors `effective_model()`.
  - `SessionHub.register_group_row()` — new `account_config_dir` param, written into
    `entry["env"]["CLAUDE_CONFIG_DIR"]` the same way `model_alias` writes `ANTHROPIC_MODEL`.
  - `SessionHub.launch()` / `SessionHub.spawn()` — new `account_config_dir` / `extra_env` params,
    needed only for the no-`session_key`-yet case (a brand-new plain session); group-row launches
    resolve it automatically through the existing `session_key`-keyed env-override chain.
  - `SettingsDialog` — new "Claude accounts" group box (`EnvEditor` reused as-is, `specs={}`),
    saved as `settings["claude_accounts"]`.
  - `NewSessionDialog`, `AgentModelEffortDialog`, `LaunchNewGroupSessionsDialog` — each gets an
    `account_combo` (Claude-only) next to its existing `model_combo`.
  - `continue_with_other_agent_for()` (the agent-handoff path) — account threaded through its
    `target == "Claude"` branches only, mirroring the existing `model`/`ANTHROPIC_MODEL` handling
    exactly (this function was flagged as fragile — 3 recent bugfix commits for model/effort
    preselect — so the account additions deliberately copy its established pattern rather than
    inventing a new one).

## Verification

- `python3 -m py_compile session_hub.py` — passes.
- Headless PyQt smoke test (`QT_QPA_PLATFORM=offscreen`, `QApplication([])`): constructed
  `AgentModelEffortDialog`, `NewSessionDialog`, `LaunchNewGroupSessionsDialog` directly — account
  combos populate from a supplied `claude_accounts` dict, preselect the given `default_account`, and
  `LaunchNewGroupSessionsDialog.rows()` returns `account_config_dir` per row correctly (`None` for
  the unset default).
- `setup_claude_account.sh ~/.claude-2` run for real — every non-isolated entry symlinked to the
  matching `~/.claude` entry (confirmed with `ls -la`); every isolated entry created fresh, JSON ones
  seeded with `{}` (not left empty, to avoid a parse error before first login).
- **Not verified — needs a manual step only the user can do**: the actual OAuth login
  (`CLAUDE_CONFIG_DIR=~/.claude-2 claude`, interactive browser flow) and a real Session Hub launch
  under account 2 to confirm the picked account is really what launches. Per project convention,
  `pytest`/repro scripts were not run against this repo (a past patch-scoping bug wiped real
  `metadata.json` this way).
- **Not verifiable from static inspection**: whether the shared `projects/` symlink causes any real
  contention once both accounts are logged in and running concurrently.

## Next step (manual, for the user)

1. `CLAUDE_CONFIG_DIR=~/.claude-2 claude` once, interactively, to log into account 2.
2. Session Hub → Settings → "Claude accounts" → rename/add rows as wanted (defaults to `Default` →
   `~/.claude` only until edited).
3. Pick an account from the new combo when launching a new session or group row.
