"""Hermetic row518 controls; no Session Hub, tmux, GUI, or network is started."""
from pathlib import Path
import tempfile
import subprocess
from codex_app_server import app_server_argv, endpoint_for, remote_tui_argv


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        endpoint = endpoint_for("group:/tmp/project#VAMP-worker5", Path(d))
        assert endpoint.parent == Path(d) and endpoint.name.endswith(".sock")
        assert app_server_argv(endpoint, "/tmp/project") == ["codex", "app-server", "--listen", f"unix://{endpoint}"]
        tui = remote_tui_argv(endpoint, "thread-123", "/tmp/project")
        assert tui[:4] == ["codex", "--remote", f"unix://{endpoint}", "--cd"]
        assert tui[-2:] == ["resume", "thread-123"]
        assert "tmux" not in " ".join(tui)
    print("[CodexAppServerCheck] PASS argv=server+remote resume endpoint=private")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
