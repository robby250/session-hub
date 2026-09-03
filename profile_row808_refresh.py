"""Hermetic before/after profile for task-2246's unchanged Running refresh."""

import argparse
from contextlib import ExitStack
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="session-hub-row808-profile-")

from PyQt6.QtWidgets import QApplication


def load_module(source_dir: Path):
    spec = importlib.util.spec_from_file_location("session_hub_profile_target", source_dir / "session_hub.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {source_dir / 'session_hub.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def profile(module, samples: int, cadence: float) -> tuple[float, float]:
    root = Path(tempfile.mkdtemp(prefix="session-hub-row808-fixture-"))
    session = module.Session(
        "Claude", "id-row808-profile", "demo", "/tmp/row808-profile", "/tmp/row808-profile",
        100, root / "transcript.jsonl",
    )
    metadata = {
        "settings": {},
        "sessions": {},
        "groups": {
            "/tmp/row808-profile": {
                "tmux": True,
                "rows": [{"name": "demo"}],
            }
        },
    }

    def provider_sessions():
        return [session]

    common = [
        patch.object(module, "read_metadata", return_value=metadata),
        patch.object(module, "discover_sessions", return_value=[]),
        patch.object(module, "codex_sessions", return_value=[]),
        patch.object(module, "claude_sessions", side_effect=provider_sessions),
        patch.object(module, "antigravity_sessions", return_value=[]),
        patch.object(module, "reconcile_tmux_desktop_env"),
        patch.object(module, "live_remote_owner_names", return_value={}),
        patch.object(module, "session_activity", return_value=("working", "")),
        patch.object(module, "compute_codex_tmux_owner_census", return_value={}),
        patch.object(module, "resolve_pending_codex_group_rows", return_value=False),
        patch.object(module, "clear_proven_codex_duplicate_bindings", return_value=False),
        patch.object(module, "codex_duplicate_row_losers", return_value=set()),
        patch.object(module, "_transcript_last_assistant_record", return_value=("", 0)),
        patch.object(module, "write_metadata"),
    ]
    if hasattr(module, "_source_change_token"):
        common.append(patch.object(module, "_source_change_token", return_value=None))
    if hasattr(module, "_directory_change_token"):
        common.append(patch.object(module, "_directory_change_token", return_value=None))
    if hasattr(module, "tmux_live_pane_snapshot"):
        common.append(patch.object(module, "tmux_live_pane_snapshot", return_value={"demo": ("%0", "1", "1")}))
    else:
        common.append(patch.object(module, "tmux_live_session_names", return_value=frozenset({"demo"})))
        common.append(patch.object(module, "tmux_pane_activity_snapshot", return_value={"demo": ("%0", "1", "1")}))

    with patch.object(module.QApplication, "platformName", return_value="xcb"):
        with ExitStack() as stack:
            for item in common:
                stack.enter_context(item)
            window = module.SessionHub()
            window._reconcile_terminal_cache = lambda _rows: None
            window._running_sessions_cache = None
            window._running_render_signature = None
            window.refresh_running_tab()
            start = time.process_time()
            wall_start = time.perf_counter()
            for index in range(samples):
                window.refresh_running_tab()
                if index + 1 < samples:
                    time.sleep(cadence)
            cpu = time.process_time() - start
            wall = time.perf_counter() - wall_start
            window.close()
    return cpu, wall


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--cadence", type=float, default=2.0)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    module = load_module(args.source_dir)
    cpu, wall = profile(module, args.samples, args.cadence)
    print(f"source={args.source_dir} samples={args.samples} cadence={args.cadence:.1f}s cpu={cpu:.6f}s wall={wall:.3f}s")


if __name__ == "__main__":
    main()
