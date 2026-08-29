# Session Hub status pipeline audit — task-2114

## User-visible failures

- The GUI Running tab showed Claude as `Working` but left a concurrently working Codex row blank.
- Session Hub and VAMPULSE peer discovery disagreed about whether Worker1 already existed, leading
  to an unnecessary resume/restart around the repository move.
- The user reports multiple status/liveness defects, so fix the complete pipeline for both Claude
  and Codex rather than special-casing the blank cell.

## Scope

Audit and fix status production, identity/liveness joins, and presentation in:

- `session_hub.py`
- `session_hub_tui.py`
- `test_session_hub.py`
- `README.md` or the existing status plan only if the contract changed

Do not edit VAMPULSE, Session Hub metadata, live transcripts, provider configuration, or running
tmux sessions. Work only in this worktree/branch and commit the result.

## Required contract

1. Define one provider-neutral state verdict consumed by GUI, TUI, JSON/CLI output and recent
   activity. At minimum: `Running` is process/tmux liveness; activity is `Working`, `Needs input`,
   `Done`, `Idle`, or unknown. Liveness and activity are separate facts.
2. Claude adapter: keep its hook evidence, but do not infer activity from X11 focus. A resumed,
   stopped, stale or duplicate Claude session must not retain another session's badge.
3. Codex adapter: `notify` only proves turn completion. Derive `Working` from durable transcript
   evidence after the last completion/status timestamp (for example a newer user-turn/input or
   in-progress transcript append); do not use mere process existence as `Working`. A completed turn
   becomes `Done`/`Idle`, not blank.
4. Identity join: exact provider + session/thread id wins. Cwd/title/name are fallback hints only.
   Two processes or transcripts sharing one cwd must not populate one row twice or steal status.
   Stale status files and stopped processes cannot appear as live activity.
5. Presentation: GUI Running and All Sessions, TUI Running and All Sessions, and `sessions_json()`/
   CLI must call the same CURRENT-activity verdict function. No provider-specific UI branch may
   silently omit the Status/Last-message fields.

   The recent-activity strip is explicitly NOT one of these callers. It is a history log of past
   `write_session_status` events, each already carrying its own recorded (state, detail, timestamp)
   - rerunning the current verdict for every historical row would silently replace "what happened,
   and when" with today's live state on each refresh. It shares only the verdict function's label/
   color vocabulary so the two surfaces render identically (task-2114 rework, corrected from an
   earlier draft of this item that wrongly folded the strip into the same requirement).
6. Refresh must be read-only apart from the existing atomic status artifact writer; rendering the
   UI must not rewrite `done` to `idle` based on which terminal happens to have focus.
7. Keep transcript reads bounded from the tail and fail closed to unknown on malformed/partial JSON.
   No full-history scan per refresh.

## Proof method

Add provider-paired fixtures and mutation-resistant assertions for:

- live + active turn => Working;
- live + completed turn => Done/Idle, never blank;
- needs-input where the provider exposes it;
- stopped process with fresh-looking status => not live/no live badge;
- stale status older than a new turn => new turn wins;
- two same-cwd sessions => exact ids get their own states, no duplicate row;
- malformed/partial last transcript line => bounded fallback, no crash;
- GUI model, TUI model and sessions JSON return identical status labels for the same fixture.

Run the targeted sequence, not the full suite: `test_continue_with_other_agent_sets_correct_target_provider`
followed in the same process by all of `SessionActivityTests` and both TUI pane test classes
(`MainPaneColumnsTests`, `RunningPaneColumnsTests`) — 22 tests, this row's own authority for what
"green" means here (row426 rework: the full suite left escaped focus-daemon threads throwing
exceptions on stderr in later tests; the targeted sequence is what proves that class of bug fixed,
and running the full suite on top adds nothing this plan currently checks). Then inspect the two
currently running real rows read-only and record the observed Claude/Codex verdicts without
stopping, relaunching or steering either session.

## Performance

Status refresh is UI cadence, but transcript parsing must be bounded by bytes/lines and only for
live rows. Cache immutable discovery results within one refresh. No process-tree-wide scan per table
cell and no unbounded JSONL read.

## Status

Implemented and landed on branch `status-audit-20260829`, not yet merged to `main`:

- `65d8cd6` — the one provider-neutral status/activity verdict for Claude+Codex (the contract's
  items 1-7 above): `session_activity()`, threaded `live_names` snapshot, transcript-tail-bounded
  Codex `Working` derivation, exact provider+id identity join.
- `1a5713b` — rework: fixed a hang, deduped tmux subprocess calls to one snapshot per refresh
  (`tmux_live_session_names()`), corrected a stale claim in this plan, TUI parity, tail-read
  escalation for oversized trailing records.
- `132520e` — rework r1: isolated two real focus-daemon threads that escaped
  `test_continue_with_other_agent_sets_correct_target_provider` and threw uncaught exceptions on
  stderr in later tests (patched the `focus_window_by_title` seam, joins+asserts every spawned
  thread dead); bounded `_codex_tail_cache` to a 256-entry LRU (was an unbounded dict keyed by
  every historical rollout path, since `codex_sessions()` has no `LIMIT`).
- (this commit) rework r2: `tmux_live_session_names()` now catches `OSError` alongside
  `TimeoutExpired` around the `tmux list-sessions` spawn (the binary vanishing/becoming
  unexecutable between `shutil.which()` and the spawn, or any other OS-level launch failure, used
  to crash the whole census instead of failing closed to an empty snapshot — same reading as "no
  tmux server running"), with a focused fixture; filled this Status section; replaced the stale
  "run the full suite" proof-method line above with the targeted 22-test authority.

22/22 targeted sequence green (21 + the new OSError/TimeoutExpired fixture), zero escaped-thread
exceptions on stderr. `git diff --check` clean. Merge to `main` is an orchestrator step, not done
by this branch itself (VAMPULSE's row428 already consumes the unified verdict via this branch's
worktree copy pending that merge — see `docs/task_notes.md#task-2114` in the VAMPULSE repo).
