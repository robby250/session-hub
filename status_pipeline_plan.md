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
5. Presentation: GUI Running and All Sessions, TUI Running and All Sessions, `sessions_json()`/CLI,
   and the recent-activity strip must call the same verdict function. No provider-specific UI branch
   may silently omit the Status/Last-message fields.
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

Run the full Session Hub test suite. Then inspect the two currently running real rows read-only and
record the observed Claude/Codex verdicts without stopping, relaunching or steering either session.

## Performance

Status refresh is UI cadence, but transcript parsing must be bounded by bytes/lines and only for
live rows. Cache immutable discovery results within one refresh. No process-tree-wide scan per table
cell and no unbounded JSONL read.

## Status

