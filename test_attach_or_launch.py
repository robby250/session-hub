"""Pure attach-or-launch CLI contract; no real tmux, agents, Qt, or GUI."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _test_sandbox  # noqa: F401  -- MUST precede session_hub; see _test_sandbox.py
import session_hub_attach as attach


def _result(code: int, *, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def _metadata(tmp: Path, *, name: str = "demo", provider: str = "Claude", session_key=None):
    return {
        "settings": {"claude_danger_mode": False, "global_env": {}, "global_flags": {}},
        "groups": {
            str(tmp): {
                "rows": [{
                    "name": name,
                    "provider": provider,
                    "session_key": session_key,
                    "override_key": f"group:{tmp}#{name}",
                }],
            },
        },
        "sessions": {},
    }


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_existing_session_is_one_probe_then_exec():
    calls = []
    attached = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return _result(0)

    code = attach.attach_or_launch(
        "demo",
        Path("/does/not/get/read"),
        run=run,
        which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
        execvp=lambda file, argv: attached.append((file, argv)),
    )
    assert code == 0
    assert calls == [["/usr/bin/tmux", "has-session", "-t", "=demo"]]
    assert attached == [("/usr/bin/tmux", ["/usr/bin/tmux", "attach-session", "-t", "=demo"])]


def test_unknown_name_fails_without_launch():
    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "metadata.json"
        _write(metadata, {"groups": {}, "sessions": {}, "settings": {}})
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return _result(1)

        try:
            attach.attach_or_launch(
                "missing",
                metadata,
                run=run,
                which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
                standalone_snapshot=lambda: {"sessions": []},
            )
        except attach.AttachError:
            pass
        else:
            raise AssertionError("unknown target unexpectedly succeeded")
        assert calls == [["/usr/bin/tmux", "has-session", "-t", "=missing"]]


def test_unknown_name_cli_returns_two():
    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "metadata.json"
        _write(metadata, {"groups": {}, "sessions": {}, "settings": {}})
        code = attach.cli(
            ["session_hub.py", "attach", "missing"],
            metadata,
            run=lambda *_args, **_kwargs: _result(1),
            which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
            standalone_snapshot=lambda: {"sessions": []},
        )
        assert code == 2


def test_missing_claude_row_launches_then_attaches():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        metadata = root / "metadata.json"
        _write(metadata, _metadata(root, name="claude-row"))
        calls = []
        attached = []

        def run(argv, **_kwargs):
            calls.append(argv)
            return _result(0 if argv[1] in {"new-session", "set-option"}
                           or (argv[1] == "has-session" and len(calls) > 3) else 1)

        code = attach.attach_or_launch(
            "claude-row",
            metadata,
            run=run,
            which=lambda name: {"tmux": "/usr/bin/tmux", "claude": "/usr/bin/claude"}.get(name),
            execvp=lambda file, argv: attached.append((file, argv)),
            standalone_snapshot=lambda: {"sessions": []},
        )
        assert code == 0
        created = next(argv for argv in calls if argv[1] == "new-session")
        assert created[0:7] == ["/usr/bin/tmux", "new-session", "-d", "-s", "claude-row", "-c", str(root)]
        assert "/usr/bin/claude" in created[-1]
        assert "--name claude-row" in created[-1]
        assert attached[-1][1][-1] == "=claude-row"


def test_missing_codex_row_uses_controller_then_attaches():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        metadata = root / "metadata.json"
        _write(metadata, _metadata(root, name="codex-row", provider="Codex", session_key="Codex:thread"))
        calls = []
        launched = []
        attached = []

        class FakeController:
            def __init__(self, path):
                assert path == metadata

            def launch_exact(self, **kwargs):
                launched.append(kwargs)

        prior = attach.SessionHubController
        attach.SessionHubController = FakeController
        try:
            def run(argv, **_kwargs):
                calls.append(argv)
                return _result(0 if len(argv) > 1 and argv[1] == "has-session" and len(calls) > 1 else 1)

            code = attach.attach_or_launch(
                "codex-row",
                metadata,
                run=run,
                which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
                execvp=lambda file, argv: attached.append((file, argv)),
            )
        finally:
            attach.SessionHubController = prior
        assert code == 0
        assert launched and launched[0]["thread_id"] == "thread"
        assert attached[-1][1][-1] == "=codex-row"


def test_ambiguous_name_and_noncanonical_name_fail():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        metadata = root / "metadata.json"
        data = _metadata(root, name="same")
        data["groups"][str(root / "other")] = data["groups"].pop(str(root))
        data["groups"][str(root)] = {"rows": [{"name": "same", "provider": "Claude"}]}
        _write(metadata, data)
        code = attach.cli(
            ["session_hub.py", "attach", "same"],
            metadata,
            run=lambda *_args, **_kwargs: _result(1),
            which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
            standalone_snapshot=lambda: {"sessions": []},
        )
        assert code == 2
        assert attach.cli(
            ["session_hub.py", "attach", "same.name"],
            metadata,
            run=lambda *_args, **_kwargs: _result(1),
            which=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
        ) == 2


if __name__ == "__main__":
    test_existing_session_is_one_probe_then_exec()
    test_unknown_name_cli_returns_two()
    test_missing_claude_row_launches_then_attaches()
    test_missing_codex_row_uses_controller_then_attaches()
    test_ambiguous_name_and_noncanonical_name_fail()
    print("attach-or-launch tests: PASS")
