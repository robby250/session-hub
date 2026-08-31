"""Pure controls for task2207; no live tmux, Codex, Qt, or Session Hub process."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import session_hub_control as control


def test_cli_module_has_no_gui_or_desktop_terminal_imports():
    source = Path(control.__file__).read_text()
    assert "QApplication" not in source
    assert "gnome-terminal" not in source
    assert "x-terminal-emulator" not in source


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
