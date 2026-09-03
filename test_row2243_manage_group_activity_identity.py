"""Pure row2243 identity-join tests: group_row_activity_status (the seam
ManageGroupDialog.reload() now calls, task-2243) must attach the REAL
session_activity() verdict -- the same join refresh_running_tab/
sessions_json_cli use -- to the exact matched session for each group row, and
never swap identities under permutation, a missing session, or a Codex row
whose live tmux owner has diverged from its saved name.

Deliberately outside the frozen Session Hub test suite, same convention as
test_row542_running_card_contract.py. Requires XDG_DATA_HOME to already point
at a disposable directory before `session_hub` is imported (see
no_unsandboxed_session_hub) -- run via:
    XDG_DATA_HOME=$(mktemp -d) python3 test_row2243_manage_group_activity_identity.py
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub


def _make_session(provider, session_id, path=None):
    return session_hub.Session(
        provider=provider, session_id=session_id, title=session_id, cwd="/tmp/proj",
        source_cwd="/tmp/proj", updated_ms=0, path=path or Path("/dev/null"),
    )


def _row(name, provider="Claude", override_key=None):
    return {"name": name, "provider": provider, "override_key": override_key or f"key-{name}"}


def _write_claude_status(session_id, state, reason=None):
    session_hub.STATUS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"state": state}
    if reason:
        payload["reason"] = reason
    (session_hub.STATUS_DIR / f"{session_id}.json").write_text(json.dumps(payload))


class GroupRowActivityIdentityTests(unittest.TestCase):
    def test_exact_identity_join_claude_two_distinct_statuses(self):
        # Two DIFFERENT sessions with two DIFFERENT real status records -- a wrong
        # key (row<->session swap) would make one of these assertions fail.
        s_a = _make_session("Claude", "sess-a")
        s_b = _make_session("Claude", "sess-b")
        _write_claude_status("sess-a", "working")
        _write_claude_status("sess-b", "needs_input", reason="agent_needs_input")
        live_names = frozenset({"alpha", "beta"})
        status_a, activity_a, _detail_a, resolved_a = session_hub.group_row_activity_status(
            _row("alpha"), "/tmp/proj", s_a, True, live_names, {}, {},
        )
        status_b, activity_b, _detail_b, resolved_b = session_hub.group_row_activity_status(
            _row("beta"), "/tmp/proj", s_b, True, live_names, {}, {},
        )
        self.assertEqual((status_a, activity_a), ("Running", "working"))
        self.assertEqual((status_b, activity_b), ("Running", "needs_input"))
        self.assertEqual(resolved_a, "alpha")
        self.assertEqual(resolved_b, "beta")

    def test_permutation_does_not_attach_one_session_status_to_another_row(self):
        s_a = _make_session("Claude", "sess-c")
        s_b = _make_session("Claude", "sess-d")
        _write_claude_status("sess-c", "done")
        _write_claude_status("sess-d", "idle")
        pairs = [(_row("gamma"), s_a), (_row("delta"), s_b)]
        live_names = frozenset({"gamma", "delta"})
        for ordering in (pairs, list(reversed(pairs))):
            results = {
                row["name"]: session_hub.group_row_activity_status(
                    row, "/tmp/proj", match, True, live_names, {}, {},
                )[1]
                for row, match in ordering
            }
            self.assertEqual(results["gamma"], "done")
            self.assertEqual(results["delta"], "idle")

    def test_missing_session_reads_stopped_unknown_not_a_stale_label(self):
        status, activity, _detail, resolved = session_hub.group_row_activity_status(
            _row("epsilon"), "/tmp/proj", None, True, frozenset(), {}, {},
        )
        self.assertEqual(status, "Stopped")
        self.assertEqual(activity, "unknown")
        self.assertEqual(resolved, "epsilon")

    def test_codex_row_resolves_through_the_registry_census_not_the_saved_name(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            s = _make_session("Codex", "rollout-1", path=Path(f.name))
            row = _row("zeta", provider="Codex", override_key="ok-zeta")
            # The row's SAVED name ("zeta") is stale; the real live tmux session is
            # "zeta-2" per the App Server owner registry -- resolved_name must be
            # the census value, not row["name"], or a renamed/restarted row reads
            # Stopped even while the Running tab shows it live.
            codex_owner_by_row_id = {"ok-zeta": "zeta-2"}
            live_names = frozenset({"zeta-2"})
            status, _activity, _detail, resolved = session_hub.group_row_activity_status(
                row, "/tmp/proj", s, True, live_names, {}, codex_owner_by_row_id,
            )
            self.assertEqual(resolved, "zeta-2")
            self.assertEqual(status, "Running")

    def test_codex_row_with_unresolvable_census_fails_closed_to_stopped(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as f:
            s = _make_session("Codex", "rollout-2", path=Path(f.name))
            row = _row("eta", provider="Codex", override_key="ok-eta")
            # Neither the registry nor the native-key census has an entry --
            # must never guess row["name"] is the live owner (task-2156 REWORK #2).
            status, activity, _detail, resolved = session_hub.group_row_activity_status(
                row, "/tmp/proj", s, True, frozenset(), {}, {},
            )
            self.assertEqual(status, "Stopped")
            self.assertEqual(activity, "unknown")
            self.assertIsNone(resolved)

    def test_stopped_row_never_shows_a_stale_activity_label(self):
        # A Claude row whose tmux session is not in live_names is Stopped
        # regardless of a leftover/stale status file on disk.
        s = _make_session("Claude", "sess-e")
        _write_claude_status("sess-e", "working")
        status, _activity, _detail, resolved = session_hub.group_row_activity_status(
            _row("theta"), "/tmp/proj", s, True, frozenset(), {}, {},
        )
        self.assertEqual(status, "Stopped")
        self.assertEqual(resolved, "theta")


class SessionsJsonCliRefactorRegressionTests(unittest.TestCase):
    """The frozen test_session_hub.py suite is blocked (no_live_tmux_or_hub_tests,
    emergency row503/task-2179) and cannot be run to confirm this row's
    sessions_json_cli refactor (inlined identity/activity logic replaced with a
    call to group_row_activity_status) preserves its existing task-2156 REWORK #2
    behavior. This ports that suite's own
    test_sessions_json_cli_ambiguous_owner_reports_stopped_not_a_sibling fixture
    verbatim as a standalone check outside the frozen suite."""

    def _fake_run_with_native_identity(self, pid_to_native_key: dict[int, str]):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        proc_root = Path(temp.name) / "proc"
        sessions_root = Path(temp.name) / "sessions"
        sessions_root.mkdir(parents=True)
        for pid, native_key in pid_to_native_key.items():
            session_id = native_key.split(":", 1)[1]
            rollout = sessions_root / f"rollout-2026-08-30T00-00-00-{session_id}.jsonl"
            rollout.write_text("{}\n")
            process = proc_root / str(pid)
            (process / "task" / str(pid)).mkdir(parents=True)
            (process / "task" / str(pid) / "children").write_text("")
            (process / "fd").mkdir()
            (process / "fd" / "9").symlink_to(rollout)
        self.enterContext(patch.object(session_hub, "PROC_ROOT", proc_root))
        self.enterContext(patch.object(session_hub, "CODEX_SESSIONS", sessions_root))
        return proc_root, sessions_root

    def test_sessions_json_cli_ambiguous_owner_still_reports_stopped_not_a_sibling(self):
        native_key = "Codex:01a00000-0000-0000-0000-0000000000ee"
        row = {"name": "tmux-dup-x", "provider": "Codex", "session_key": native_key}
        session_hub.METADATA_PATH.write_text(
            json.dumps({
                "settings": {}, "sessions": {},
                "groups": {"/tmp/vamp": {"tmux": True, "rows": [row]}},
            }),
            encoding="utf-8",
        )
        live_session = session_hub.Session(
            "Codex", native_key.split(":", 1)[1], "w", "/tmp/vamp", "/tmp/vamp", 100,
            Path("/tmp/w.jsonl"),
        )
        self._fake_run_with_native_identity({701: native_key, 702: native_key})

        def fake_run(argv, **kwargs):
            result = MagicMock(returncode=0, stdout="")
            if argv[1] == "list-sessions":
                result.stdout = "tmux-dup-x\ntmux-dup-y\n"
            elif argv[1] == "list-panes":
                result.stdout = "tmux-dup-x\t%0\t701\t1788000000\ntmux-dup-y\t%1\t702\t1788000000\n"
            return result

        with (
            patch.object(session_hub, "codex_sessions", return_value=[live_session]),
            patch.object(session_hub, "claude_sessions", return_value=[]),
            patch.object(session_hub, "antigravity_sessions", return_value=[]),
            patch.object(session_hub.shutil, "which", return_value="/usr/bin/tmux"),
            patch.object(session_hub.subprocess, "run", side_effect=fake_run),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                session_hub.sessions_json_cli()
            payload = json.loads(out.getvalue())
        status = payload["groups"]["/tmp/vamp"]["rows"][0]["status"]
        self.assertEqual(status, "Stopped")


if __name__ == "__main__":
    unittest.main()
