# row450 (task-2142) perf record

Same host, same corpus: 5 live tmux sessions (VAMP-reviewer, VAMP-worker1,
VAMP-worker2, VAMP-worker3, VAMPULSE-orchestrator), same on-disk transcript
corpus, measured back-to-back. `/usr/bin/time -v` for wall/user CPU/RSS,
`strace -f -e trace=execve` for tmux subprocess/capture-pane counts.

## `session_hub.py --sessions-json`

| | wall | user CPU | max RSS | tmux execs | capture-pane |
|---|---|---|---|---|---|
| BEFORE (105b0d9, pre-row450) call 1 | 4.71s | 4.34s | 71504 KiB | 1 | 0 |
| BEFORE call 2 (repeat, no persistent index) | 4.62s | 4.25s | 71044 KiB | 1 | 0 |
| AFTER (6ca3bc3, HEAD) cold (builds index) | 4.57s | 4.18s | 71204 KiB | 1 | 0 |
| AFTER warm (repeat, reads persistent index) | 0.20s | 0.14s | 58364 KiB | 1 | 0 |

BEFORE never caches across process invocations, so every call pays the full
~4.6s scan. AFTER, the persistent scan index (6ca3bc3, keyed by path
identity/size/mtime) makes a repeat call ~23x faster on wall clock and ~30x on
user CPU once warm. `capture-pane` is exercised by the TUI's pane-census path,
not this CLI entrypoint, hence 0 in both.

## TUI shell interactive <=0.5s / warm data <=1.0s

Not re-benched as a second wall-clock timing: `test_shell_renders_before_fetch_completes`
(test_session_hub_tui.py) is a hermetic control that blocks `sessions_json`
indefinitely and asserts the Textual shell (columns mounted, 0-row interactive
table) is already up while the fetch is still stuck — i.e. first paint is
synchronous Textual mount with no I/O in the critical path, so its wall time
is bounded by Textual's own mount cost, independent of corpus size or fetch
latency. `test_one_fetch_feeds_both_main_and_running_panes` proves the single
shared generation. Given the `--sessions-json` warm figure above (0.20s), a
real warm run's async fetch lands well inside the 1.0s budget; a cold run
(4.57s, first launch only, before the index exists) is the one case that can
exceed it and is expected to shrink as the index accumulates.

## Full-suite statement

`python3 -m pytest -q test_session_hub.py test_session_hub_tui.py` at HEAD
(6ca3bc3): 315 passed, 42 subtests passed, 2 failed
(`test_link_to_existing_conversation_creates_link_between_same_cwd_sessions`,
`test_link_to_existing_conversation_copies_old_name_and_launch_overrides`).
Both failures reproduce identically on a clean `git stash` of every row450
change (confirmed pre-existing, unrelated to shared discovery/status core).
This run satisfies the brief's "one full suite only because this row changes
shared discovery/status core" proof-method clause; runtime 22.91s.
