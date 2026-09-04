> STATUS: decided 2026-09-04 — ACTIVE

# Codex App Server row identity — linking, Running visibility, launch lockout

## Context

`VAMP-orch1` was swapped Claude → Codex with **Continue with other agent**. The new Codex session
never linked (it showed up as a separate row in All Sessions); a manual **Link with existing
conversation** fixed metadata but the row then vanished from the Running tab; double-clicking it
refused to launch; and a peer send to the name was refused as owner-ambiguous. The tmux session was
alive the whole time and reachable externally.

User: *"i'll do some tests with you to confirm that sessions link correctly after you do the fixes
so we can fix this for good, and hopefully avoid any link conflicts in the future as well because
there have been a lot of issues with the linking system in this program"*

## The finding

All of it is one false premise: **the Codex identity machinery predates App Server (`codex --remote`)
mode and still assumes the tmux pane process holds the rollout JSONL open.** It does not.

```
pane 15972 = codex --remote unix://…/c-5707….sock --cd …/VAMPULSE-game   → no rollout fd
app-server 11119 (ppid 4834 = the session_hub.py GUI, start_new_session=True)
                                                        → fd: …/rollout-…-01a06b46-….jsonl
```

`_codex_native_key_from_pids` (session_hub.py) walks a pane pid *and its /proc descendants*. The
app-server is a detached **sibling**, so that walk returns `None` for every App Server row.

**Verified** from live `/proc`, the runtime registry, `tmux`, and a read-only
`--status-group-row` run:

| # | cause | file |
|---|---|---|
| RC1 | the swap's Codex branch was the only `self.launch(...)` site omitting `session_key`, so the owner record was filed under the **bare tmux name** instead of the row's `override_key` | session_hub.py `continue_with_other_agent_for` |
| RC2 | `codex_tmux_native_key` → `None` ⇒ `resolve_pending_links` never resolved, retried to its 15-min expiry | session_hub.py `resolve_pending_links` |
| RC3 | the Running tab's registry lookup is by `override_key` (missed RC1's record) and its census fallback was RC2's `None`, which `continue`s — the row silently disappears | session_hub.py `refresh_running_tab` |
| RC4 | a non-`Codex:` `session_key` was passed through as a Codex thread id, building `codex --remote … resume Claude:<uuid>`; that remote died at startup and stranded its app-server | session_hub_control.py `_thread_id`, session_hub_attach.py `attach_or_launch` |
| RC5 | `launch_exact` (and `stop`) failed closed forever: the dead owner's window was gone, but a **different** record's reserved window in the same tmux session made `_remote_identity_can_be_recreated` refuse | session_hub_control.py |
| RC6 | two live records with the same `aliases` ⇒ peer addressing refused the name; `live_remote_owner_names` deduped by `row_id` only | codex_app_server.py |

Live proof of the split identity:

| record | `row_id` | server | remote | rollout |
|---|---|---|---|---|
| `c-5707…` | `VAMP-orch1` (bare name) | 11119 live | 15972 live | `Codex:01a06b46-…` |
| `c-fee7…` | `group:…#VAMP-orch1` | 15200 live | 15334 **dead** | none |

Reproduced read-only before the fix:

```
$ python3 session_hub.py --status-group-row …/VAMPULSE-game VAMP-orch1
{"message": "owned Codex remote window is missing or mismatched", …, "status": "error"}
$ tmux list-windows -t '=VAMP-orch1' -F '#{window_id}\t#{window_name}'
@6	__session_hub_codex_remote__
```

**Hypothesis, not load-bearing:** the exact trigger for the second launch (most likely a
`session-hub attach VAMP-orch1` in the gap between the first remote exiting and the second
starting). RC4 is proven from the record's own contents regardless of caller.

## What shipped

- **F1** `continue_with_other_agent_for` passes `session_key` (Codex only — for Antigravity the
  same argument would newly apply the source session's env/flag overrides).
- **F2** `open_rollout_keys` / `live_owner_records` in codex_app_server.py, merged into
  `compute_codex_tmux_owner_census` and consulted first by `codex_tmux_native_key`. The owner
  record already names the exact app-server pid, so ownership needs no discovery.
- **F3** `_thread_id` and `attach_or_launch` return `None` for any non-`Codex:` key.
- **F4** `retire_orphan_owner` — a record whose remote is dead by start time *and* whose app-server
  holds no rollout is retired and its server stopped, from both `launch_exact` and `stop`.
  `live_remote_owner_names` now drops duplicate **names**, not just duplicate row ids.
- **F5** `reconcile_codex_owner_threads` — `/clear` relink for group **and** standalone rows.
- **F6** the swap marks the group row `codex_pending_since` and drops the stale Claude
  `session_key`, so it fails closed instead of rendering the dead Claude session.
- Registry reads now go through `session_hub.REGISTRY_DIR`. `test_codex_app_server` already patched
  that name against a nonexistent attribute — the patch raised instead of isolating, so those
  launch controls were reading the real runtime registry.

## Decisions

| question | answer |
|---|---|
| Repair the stuck row how? | Stop clean and relaunch (not an in-place record re-key) |
| Two open rollouts under one app-server? | Newest rollout mtime wins |
| Retire an orphaned owner record automatically? | Yes, in `launch_exact` and in `stop` |
| `/clear` relink scope? | Group **and** standalone, anchored on the owner record's process lifetime |
| Who implements | Inline, one pass — no persistent session-hub worker session existed |

### Why the record, not the row, anchors F5

A standalone Codex record's `row_id` **is** its native key (`resume_session` passes
`session_key=session.key`), so a `/clear` invalidates the thing we would key on; a group row's
`override_key` is stable but `rename_group_row_in` rewrites it without touching the runtime record.
Anchoring on one record's start-time-bound process lifetime — `prev = record["thread_id"]` vs the
newest rollout fd open on `record["pid"]` — removes every heuristic. A reused pid cannot inherit
another process's `prev` because `live_record` binds pid to start time.

Stated limits: a `thread/fork` is indistinguishable from a `/clear` (both are continuations, so
both link — correct); two `/clear`s while Session Hub is closed link first→last and skip the
middle thread; the record lives in `/run`, so it survives an app restart but not a reboot.

## Rejected

- **Query the app-server over its socket for the current thread.** `thread/start`, `thread/resume`
  and `thread/list` are real methods
  ([app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
  [thread/list paginates session_index.jsonl](https://gist.github.com/oneryalcin/ee2c27e2d8aa040da8fbe7eebcc2ecea)),
  but a JSON-RPC round-trip per row inside a GUI refresh tick is a latency and blocking risk and
  makes identity depend on protocol compatibility. The open rollout fd is exact and costs one
  `/proc` read.
- **Read `~/.codex/sessions/**/session_index.jsonl`.** It lists threads, not which tmux row or
  app-server owns one; ownership is the missing edge.
- **Fail closed when an app-server holds two rollouts.** Reproduces the "row vanished from Running"
  symptom being fixed and makes `/clear` undetectable.
- **In-place re-key of the live owner record** to un-stick `VAMP-orch1` — user chose a clean stop
  and relaunch.
- **Key the `/clear` relink on `row["session_key"]` / `row_id`** — see above.

## Verification

- `py_compile` on all four modules and both touched test modules: clean.
- The read-only probe that proved RC5 flips: `--status-group-row … VAMP-orch1` went
  `"status": "error"` → `"status": "stopped"` (launchable). The still-live `VAMP-work1` row stayed
  `"status": "running"` throughout — a negative control that the repair was scoped.
- `live_owner_records` + `open_rollout_keys` resolve the live `VAMP-work1` record to
  `Codex:01a04cd5-…` through its app-server pid — the exact edge the pane walk cannot see.
- **Not executed:** the test suite. `feedback_no_tests.md` forbids running pytest or ad hoc repro
  scripts in this project; new controls are written but unrun.
- **Cannot be verified without the user:** that the Running row is clickable and embeds, that a
  fresh Continue-with-other-agent links within one refresh, and that a `/clear` inside a Codex row
  relinks.
