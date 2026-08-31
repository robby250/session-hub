"""Pure controls for task2207; no live tmux, Codex, Qt, or Session Hub process."""
from __future__ import annotations

import json
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import session_hub_control as control


def test_cli_module_has_no_gui_or_desktop_terminal_imports():
    source = Path(control.__file__).read_text()
    assert "QApplication" not in source
    assert "gnome-terminal" not in source
    assert "x-terminal-emulator" not in source


def test_app_server_spawn_is_detached_and_stdio_inert():
    source = Path(control.__file__).read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "popen"]
    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords}
    assert {"stdin", "stdout", "stderr", "close_fds", "start_new_session"} <= keywords.keys()
    assert all(isinstance(keywords[name], ast.Attribute) and keywords[name].attr == "DEVNULL"
               for name in ("stdin", "stdout", "stderr"))
    assert all(isinstance(keywords[name], ast.Constant) and keywords[name].value is True
               for name in ("close_fds", "start_new_session"))

    gui_source = Path(__file__).with_name("session_hub.py").read_text()
    gui_tree = ast.parse(gui_source)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
        and {item.arg for item in node.keywords} >=
        {"stdin", "stdout", "stderr", "close_fds", "start_new_session"}
        for node in ast.walk(gui_tree)
    )


def test_group_stop_paths_use_controller_for_codex():
    source = Path(__file__).with_name("session_hub.py").read_text()
    tree = ast.parse(source)
    for function_name in ("stop_group_row", "stop_selected_running", "stop_group_row_cli"):
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == function_name)
        expected = "stop_group_row" if function_name == "stop_selected_running" else "stop"
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == expected
            for node in ast.walk(function)
        ), function_name


def test_remote_identity_validation_and_exact_creation_cleanup_are_pure():
    controller = control.SessionHubController(Path("/tmp/unused-metadata"), which=lambda _: "/usr/bin/tmux")
    controller._windows = lambda _session: [("@7", control.REMOTE_WINDOW)]
    controller._remote_pid = lambda _session, _window: 123
    with patch.object(control, "_start_time", return_value="start-123"):
        owner = {
            "remote_window_id": "@7", "remote_pid": 123, "remote_start_time": "start-123"
        }
        controller._validate_remote_identity("worker", owner)
        try:
            controller._validate_remote_identity(
                "worker", {**owner, "remote_pid": 456}
            )
        except control.ControlError:
            pass
        else:
            raise AssertionError("replaced remote PID was accepted")

    calls = []
    controller._windows = lambda _session: [("@9", control.REMOTE_WINDOW), ("@1", "composer")]
    controller._tmux = lambda *args, **kwargs: calls.append((args, kwargs))
    controller._remove_created_remote_window("worker", "@9")
    assert calls == [(("kill-window", "-t", "=worker:@9"), {"check": False})]

    controller._windows = lambda _session: [("@4", control.REMOTE_WINDOW)]
    try:
        controller._ensure_remote_window("worker", "/tmp", Path("/tmp/e.sock"), None)
    except control.ControlError as error:
        assert "without an owner" in str(error)
    else:
        raise AssertionError("ownerless reserved window was adopted")

    for invalid in (
        {"remote_window_id": "@7", "remote_pid": 123},
        {"remote_window_id": "@8", "remote_pid": 123, "remote_start_time": "start-123"},
        {"remote_window_id": "@7", "remote_pid": 456, "remote_start_time": "start-123"},
        {"remote_window_id": "@7", "remote_pid": 123, "remote_start_time": "reused"},
    ):
        try:
            controller._validate_remote_identity("worker", invalid)
        except control.ControlError:
            pass
        else:
            raise AssertionError(f"invalid remote identity was accepted: {invalid}")


def test_dead_owned_remote_client_is_recreated_on_launch(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"groups": {"/tmp": {"rows": [
        {"name": "worker", "provider": "Codex", "override_key": "group:/tmp#worker",
         "session_key": "Codex:thread-1"}
    ]}}}))
    record_path = tmp_path / "registry" / "owner.json"
    owner = {
        "endpoint": str(record_path.with_suffix(".sock")), "thread_id": "thread-1",
        "remote_window_id": "@7", "remote_pid": 123, "remote_start_time": "old-start",
    }
    controller = control.SessionHubController(metadata, registry_dir=record_path.parent)
    controller._owner_path = lambda _row_id: record_path
    controller._windows = lambda _session: []
    created = []
    controller._ensure_remote_window = lambda *args, **kwargs: (
        created.append((args, kwargs)) or ("@9", True)
    )
    controller._remote_pid = lambda _session, _window: 456
    controller._save_remote_identity = lambda _path, **kwargs: {
        **owner, "remote_window_id": kwargs["window_id"], "remote_pid": kwargs["pid"],
    }
    with patch.object(control, "live_record", return_value=owner), \
         patch.object(control, "_start_time", return_value=""):
        result = controller.launch("/tmp", "worker", resume=True)

    assert result["status"] == "resumed"
    assert result["thread_id"] == "thread-1"
    assert result["window_created"] is True
    assert created[0][1]["allow_existing"] is False


def test_live_or_ambiguous_remote_identity_is_not_recreated(tmp_path):
    controller = control.SessionHubController(tmp_path / "metadata.json")
    owner = {
        "remote_window_id": "@7", "remote_pid": 123, "remote_start_time": "start-123",
    }
    controller._windows = lambda _session: [("@8", control.REMOTE_WINDOW)]
    with patch.object(control, "_start_time", return_value=""):
        assert controller._remote_identity_can_be_recreated("worker", owner) is False
    controller._windows = lambda _session: []
    with patch.object(control, "_start_time", return_value="start-123"):
        assert controller._remote_identity_can_be_recreated("worker", owner) is False


def test_exact_tmux_target_preserves_legacy_windows(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"groups": {"/tmp": {"rows": [
        {"name": "worker", "provider": "Codex", "override_key": "group:/tmp#worker"}
    ]}}}))
    calls = []
    windows = [("@0", "composer")]

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "has-session":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "list-windows":
            return SimpleNamespace(returncode=0, stdout="@0\tcomposer\n", stderr="")
        if argv[1] == "list-panes":
            return SimpleNamespace(returncode=0, stdout="123\n", stderr="")
        if argv[1] == "new-window":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    class FakeProcess:
        pid = 999

        def terminate(self):
            pass

    controller = control.SessionHubController(metadata, registry_dir=tmp_path / "registry",
                                               run=run, popen=lambda *a, **k: FakeProcess(),
                                               which=lambda _: "/usr/bin/tmux")
    with patch.object(control, "endpoint_for", return_value=tmp_path / "registry" / "c.sock"), \
         patch.object(control, "wait_ready", return_value=True), \
         patch.object(control, "publish_record", return_value={"thread_id": ""}), \
         patch.object(control, "_start_time", return_value="999"):
        # The injected fake only models the tmux boundary; publication itself is separately
        # covered by test_codex_app_server.py.
        try:
            controller.launch("/tmp", "worker")
        except (control.ControlError, FileNotFoundError):
            pass
    assert any(argv[1:5] == ["new-window", "-d", "-t", "=worker"] for argv in calls)
    assert not any(argv[1] == "kill-session" for argv in calls)
    assert not any(argv[1] == "rename-session" for argv in calls)


def test_bad_row_and_wrong_provider_fail_closed(tmp_path):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"groups": {"/tmp": {"rows": [
        {"name": "claude", "provider": "Claude"}
    ]}}}))
    c = control.SessionHubController(metadata, run=lambda *a, **k: None, which=lambda _: None)
    try:
        c.status("/tmp", "claude")
    except control.ControlError as error:
        assert "Codex" in str(error)
    else:
        raise AssertionError("wrong-provider row was accepted")
