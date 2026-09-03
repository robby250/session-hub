"""Pure row518 controls; no Session Hub, tmux, GUI, or network is started."""
from __future__ import annotations

import ast
import inspect
import json
import os
import socket
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import codex_app_server
from codex_app_server import (
    SCHEMA_VERSION,
    app_server_argv,
    publish_record,
    endpoint_for,
    discard_stale_record,
    live_record,
    record_for_row,
    remote_tui_argv,
    stop_owned,
)


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.terminated = False

    def terminate(self):
        self.terminated = True


def _fake_server(endpoint: Path) -> subprocess.Popen:
    """Start a local Unix listener; no Codex, Qt, tmux, or network is involved."""
    code = (
        "import socket, sys, time\n"
        "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.bind(sys.argv[1]); s.listen(); s.settimeout(0.1)\n"
        "while True:\n"
        "  try: c,_=s.accept(); c.close()\n"
        "  except TimeoutError: pass\n"
    )
    return subprocess.Popen([os.environ.get("PYTHON", "python3"), "-c", code, str(endpoint)])


def _stop_fake_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _fake_lifecycle_control() -> None:
    """Exercise readiness, atomic records, restart identity, and stale cleanup with fakes."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        endpoint_a = endpoint_for("row-a", root)
        endpoint_b = endpoint_for("row-b", root)
        assert endpoint_a != endpoint_b and endpoint_a.parent == endpoint_b.parent == root
        # Real managed keys include long provider/session/group identities.  The endpoint name
        # must remain bounded by AF_UNIX SUN_LEN while the complete key stays in the JSON record.
        long_row_id = "Codex:" + "session-identity-" * 24
        long_endpoint = endpoint_for(long_row_id, root)
        assert len(os.fsencode(long_endpoint)) < 104
        assert long_row_id not in long_endpoint.name
        assert long_endpoint != endpoint_for(long_row_id, root)
        server_a = _fake_server(endpoint_a)
        server_b = _fake_server(endpoint_b)
        record_a = endpoint_a.with_suffix(".json")
        record_b = endpoint_b.with_suffix(".json")
        try:
            assert codex_app_server.wait_ready(endpoint_a, timeout=1.0)
            assert codex_app_server.wait_ready(endpoint_b, timeout=1.0)
            observed_partial = []
            observing = True

            def observe_publication():
                while observing:
                    if record_a.exists():
                        try:
                            json.loads(record_a.read_text())
                        except json.JSONDecodeError:
                            observed_partial.append(True)

            observer = threading.Thread(target=observe_publication)
            observer.start()
            saved_a = publish_record(record_a, row_id="row-a", endpoint=endpoint_a,
                                     thread_id="thread-a", process=server_a,
                                     name="VAMP-worker5", aliases=["VAMP-worker5"])
            observing = False
            observer.join(timeout=1)
            saved_b = publish_record(record_b, row_id="row-b", endpoint=endpoint_b,
                                     thread_id="thread-b", process=server_b)
            assert not observed_partial
            assert record_a.exists() and record_b.exists()
            # Cross-component contract: the private stable row id and the public managed-row
            # target coexist, so session_ctl can resolve the target name without guessing.
            assert saved_a["name"] == "VAMP-worker5"
            assert saved_a["aliases"] == ["VAMP-worker5"]
            assert not list(root.glob(".*.tmp")), "atomic publication leaked a temp record"
            assert remote_tui_argv(endpoint_a, saved_a["thread_id"], "/tmp")[-2:] == ["resume", "thread-a"]
            assert remote_tui_argv(endpoint_a, None, "/tmp") == [
                "codex", "--remote", f"unix://{endpoint_a}", "--cd", "/tmp"
            ]

            # Restart gets a new private endpoint but reuses the saved thread id exactly.
            stop_owned(record_a, row_id="row-a")
            endpoint_a2 = endpoint_for("row-a", root)
            server_a2 = _fake_server(endpoint_a2)
            record_a2 = endpoint_a2.with_suffix(".json")
            try:
                assert endpoint_a2 != endpoint_b and codex_app_server.wait_ready(endpoint_a2, timeout=1.0)
                restarted = publish_record(record_a2, row_id="row-a", endpoint=endpoint_a2,
                                            thread_id=saved_a["thread_id"], process=server_a2)
                assert remote_tui_argv(endpoint_a2, restarted["thread_id"], "/tmp")[-1] == "thread-a"
            finally:
                stop_owned(record_a2, row_id="row-a")

            # A readiness failure publishes nothing and therefore cannot launch a remote client.
            missing = endpoint_for("row-missing", root)
            remote_calls = []
            if codex_app_server.wait_ready(missing, timeout=0.05):
                remote_calls.append(remote_tui_argv(missing, None, "/tmp"))
            assert remote_calls == [] and not missing.with_suffix(".json").exists()

            # Binding validation permits stale cleanup but never accepts an ambiguous owner set.
            stale_endpoint = endpoint_for("row-stale", root)
            stale_record = stale_endpoint.with_suffix(".json")
            stale_endpoint.touch()
            stale_record.write_text(json.dumps({
                "schema": SCHEMA_VERSION, "row_id": "row-stale",
                "endpoint": str(stale_endpoint), "thread_id": "thread-stale",
                "pid": os.getpid(), "start_time": "",
            }))
            assert codex_app_server.discard_stale_record(stale_record, row_id="row-stale")
            assert not stale_record.exists() and stale_endpoint.exists()
            stale_endpoint.unlink()
            first = root / "ambiguous-1.json"
            second = root / "ambiguous-2.json"
            for path in (first, second):
                endpoint = path.with_suffix(".sock")
                endpoint.touch()
                path.write_text(json.dumps({
                    "schema": SCHEMA_VERSION, "row_id": "row-ambiguous",
                    "endpoint": str(endpoint), "pid": os.getpid(), "start_time": "",
                }))
            try:
                record_for_row("row-ambiguous", root)
            except RuntimeError:
                pass
            else:
                raise AssertionError("ambiguous owner set was accepted")
            assert first.exists() and second.exists()

            # Malformed/scalar and wrong-shaped records are ownership errors, never "no owner".
            bad_root = root / "malformed"
            bad_root.mkdir()
            for bad in ("[]", json.dumps({"schema": SCHEMA_VERSION, "row_id": "same-row"}),
                        json.dumps({"schema": SCHEMA_VERSION, "row_id": "same-row",
                                    "endpoint": str(root / "bad.sock"), "thread_id": "t",
                                    "pid": "not-an-int", "start_time": "1"})):
                bad_path = bad_root / "bad.json"
                bad_path.write_text(bad)
                try:
                    record_for_row("same-row", bad_root)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("malformed owner record was treated as absent")
                bad_path.unlink()
        finally:
            observing = False
            for process in (server_a, server_b):
                _stop_fake_server(process)


def _fake_session_hub_launch_control() -> None:
    """Drive SessionHub's launch boundary with fakes, without Qt, Codex, or a terminal."""
    import session_hub

    class FakeHub:
        def __init__(self):
            self._codex_app_servers = {}
            self.stopped = []
            self.spawned = []

        def stop_codex_app_server(self, row_id):
            self.stopped.append(row_id)

        def spawn(self, command, session_key, **kwargs):
            self.spawned.append((command, session_key, kwargs))

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        events = []
        endpoints = [root / "row-a.sock", root / "row-a-restart.sock"]
        records = []

        def fake_endpoint(row_id):
            endpoint = endpoints[len(records)]
            events.append(("endpoint", row_id, endpoint.name))
            return endpoint

        def fake_popen(argv, **kwargs):
            events.append(("start", tuple(argv), kwargs["cwd"]))
            return FakeProcess(os.getpid())

        def fake_ready(endpoint):
            events.append(("ready", endpoint.name))
            return True

        def fake_publish(path, **kwargs):
            events.append(("publish", kwargs["row_id"], kwargs["thread_id"]))
            records.append({"row_id": kwargs["row_id"], "name": kwargs.get("name"),
                            "aliases": kwargs.get("aliases")})
            return {"row_id": kwargs["row_id"], "thread_id": kwargs["thread_id"]}

        def fake_remote(endpoint, thread_id, cwd):
            events.append(("remote", endpoint.name, thread_id, cwd))
            return ["codex", "--remote", str(endpoint)]

        hub = FakeHub()
        with (
            patch.object(session_hub, "REGISTRY_DIR", root),
            patch.object(session_hub, "endpoint_for", side_effect=fake_endpoint),
            patch.object(session_hub.subprocess, "Popen", side_effect=fake_popen),
            patch.object(session_hub, "wait_ready", side_effect=fake_ready),
            patch.object(session_hub, "publish_record", side_effect=fake_publish),
            patch.object(session_hub, "remote_tui_argv", side_effect=fake_remote),
            patch.object(session_hub.shutil, "which", return_value="/usr/bin/gnome-terminal"),
        ):
            session_hub.SessionHub._launch_codex_app_server(
                hub, "thread-a", "/tmp/project", "/tmp/project", "gpt", "row-a", "tmux-a",
                "medium", "prompt", False,
            )
            # Restart through the same production boundary: exact thread id, new private endpoint.
            session_hub.SessionHub._launch_codex_app_server(
                hub, "thread-a", "/tmp/project", "/tmp/project", "gpt", "row-a", "tmux-a",
                "medium", "prompt", False,
            )
        phases = [event[0] for event in events]
        assert phases == ["endpoint", "start", "ready", "publish", "remote",
                          "endpoint", "start", "ready", "publish", "remote"]
        assert events[3] == ("publish", "row-a", "thread-a")
        assert events[8] == ("publish", "row-a", "thread-a")
        assert records[0]["name"] == "tmux-a" and records[0]["aliases"] == ["tmux-a"]
        assert hub.stopped == ["row-a", "row-a"]
        assert len(hub.spawned) == 2 and all(item[1] == "row-a" for item in hub.spawned)

    # The real stop path must surface an ambiguous registry instead of swallowing it and launching.
    ambiguous = session_hub.SessionHub.__new__(session_hub.SessionHub)
    ambiguous._codex_app_servers = {}
    with patch.object(session_hub, "stop_owned_for_row", side_effect=RuntimeError("ambiguous")), \
         patch.object(session_hub, "record_for_row", side_effect=RuntimeError("ambiguous")):
        try:
            session_hub.SessionHub.stop_codex_app_server(ambiguous, "row-a")
        except RuntimeError as error:
            assert "ambiguous" in str(error)
        else:
            raise AssertionError("SessionHub swallowed ambiguous persisted ownership")


def _record(path: Path, endpoint: Path, row_id: str):
    return codex_app_server.publish_record(
        path,
        row_id=row_id,
        endpoint=endpoint,
        thread_id="thread-123",
        process=FakeProcess(os.getpid()),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        endpoint = endpoint_for("group:/tmp/project#VAMP-worker5", root)
        endpoint.touch()
        record_path = endpoint.with_suffix(".json")
        assert endpoint.parent == root and endpoint.name.endswith(".sock")
        assert app_server_argv(endpoint, "/tmp/project") == [
            "codex", "app-server", "--listen", f"unix://{endpoint}"
        ]
        tui = remote_tui_argv(endpoint, "thread-123", "/tmp/project")
        assert tui[:4] == ["codex", "--remote", f"unix://{endpoint}", "--cd"]
        assert tui[-2:] == ["resume", "thread-123"]
        # A restart rebuilds argv from the same saved thread, never a replacement id.
        assert remote_tui_argv(endpoint, "thread-123", "/tmp/project") == tui
        assert remote_tui_argv(endpoint, None, "/tmp/project") == [
            "codex", "--remote", f"unix://{endpoint}", "--cd", "/tmp/project"
        ]
        assert "tmux" not in " ".join(tui)

        record = _record(record_path, endpoint, "group:/tmp/project#VAMP-worker5")
        assert record["schema"] == SCHEMA_VERSION
        assert json.loads(record_path.read_text())["thread_id"] == "thread-123"
        assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
        assert live_record(record_path, row_id=record["row_id"])["endpoint"] == str(endpoint)
        assert record_for_row(record["row_id"], root) == record_path

        # Cross-row endpoint reuse is rejected in both directions: the record path and
        # endpoint basename are an exact local ownership binding.
        forged = dict(record, endpoint=str(root / "other-row-deadbeef.sock"))
        record_path.write_text(json.dumps(forged))
        try:
            live_record(record_path, row_id=record["row_id"])
        except RuntimeError as error:
            assert "endpoint" in str(error)
        else:
            raise AssertionError("cross-row endpoint mutation was accepted")
        record_path.write_text(json.dumps(record))

        # Missing PID identity is never a match, even if a stale endpoint remains.
        record_path.write_text(json.dumps(dict(record, start_time="")))
        try:
            live_record(record_path, row_id=record["row_id"])
        except RuntimeError as error:
            assert "stale" in str(error)
        else:
            raise AssertionError("empty start-time owner was accepted")
        record_path.write_text(json.dumps(record))

        # stop_owned validates the same record before killing and removes only this row's
        # endpoint/record. os.kill is patched so this pure control cannot kill the test.
        with patch.object(codex_app_server.os, "kill") as kill:
            stop_owned(record_path, row_id=record["row_id"])
            kill.assert_called_once_with(os.getpid(), codex_app_server.signal.SIGTERM)
        assert not record_path.exists() and not endpoint.exists()

        # Two records claiming one row are ambiguous and therefore un-stoppable.
        one = root / "row-a-11111111.json"
        two = root / "row-a-22222222.json"
        for path in (one, two):
            sock = path.with_suffix(".sock")
            sock.touch()
            _record(path, sock, "same-row")
        try:
            record_for_row("same-row", root)
        except RuntimeError as error:
            assert "Multiple" in str(error)
        else:
            raise AssertionError("ambiguous owner records were accepted")

    _fake_lifecycle_control()
    _fake_session_hub_launch_control()

    source = inspect.getsource(codex_app_server)
    assert "wait_ready" in source and "not record[\"start_time\"]" in source
    assert "endpoint.name != f\"{path.stem}.sock\"" in source
    assert "os.replace(tmp, path)" in source and "discard_stale_record" in source

    # Mutation control: readiness must precede publication and remote-client launch.
    hub_source = Path(__file__).with_name("session_hub.py").read_text()
    hub_tree = ast.parse(hub_source)
    launch = next(node for node in ast.walk(hub_tree)
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "_launch_codex_app_server")
    lines = {
        call.func.id: call.lineno
        for call in ast.walk(launch)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        and call.func.id in {"wait_ready", "publish_record", "remote_tui_argv"}
    }
    assert lines["wait_ready"] < lines["publish_record"] < lines["remote_tui_argv"]
    funcs = {
        node.name: node for node in ast.walk(hub_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "stop_codex_app_server"
        for call in ast.walk(funcs["stop_selected_running"])
        if isinstance(call, ast.Call)
    )
    assert any(
        isinstance(call.func, ast.Name) and call.func.id == "stop_owned_for_row"
        for name in ("stop_group_row_cli", "stop_session_cli")
        for call in ast.walk(funcs[name])
        if isinstance(call, ast.Call)
    )
    # Fresh managed-group launches must carry the same public row name as resume launches; an
    # override key alone is private and cannot be resolved by the dependent sender.
    fresh = funcs["launch_group_row"]
    fresh_calls = [call for call in ast.walk(fresh)
                   if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                   and call.func.attr == "launch"]
    assert any(
        any(k.arg == "tmux_name" and isinstance(k.value, ast.Subscript)
            and isinstance(k.value.slice, ast.Constant) and k.value.slice.value == "name"
            for k in call.keywords)
        for call in fresh_calls
    ), "fresh managed launch dropped its public row name"
    assert "stop_all_codex_app_servers" in hub_source
    print("[CodexAppServerCheck] PASS fake readiness/failure unique-endpoint restart-thread atomic-publication stale-cleanup fresh-row-client lifecycle-owned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
