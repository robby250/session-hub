"""Pure row518 controls; no Session Hub, tmux, GUI, or network is started."""
from __future__ import annotations

import ast
import inspect
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import codex_app_server
from codex_app_server import (
    SCHEMA_VERSION,
    app_server_argv,
    endpoint_for,
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

    source = inspect.getsource(codex_app_server)
    assert "wait_ready" in source and "not record.get(\"start_time\")" in source
    assert "endpoint.name != f\"{path.stem}.sock\"" in source

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
    assert "stop_all_codex_app_servers" in hub_source
    print("[CodexAppServerCheck] PASS readiness-before-publish exact-thread atomic-registry stale/cross-row-fail-closed lifecycle-owned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
