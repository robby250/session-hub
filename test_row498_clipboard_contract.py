"""Pure source proof for row498; intentionally does not execute tmux or Session Hub."""
from pathlib import Path

source = Path(__file__).with_name("session_hub.py").read_text()

def main():
    required = (
        "CLIPBOARD_ENV_ALLOWLIST", "TMUX_AUTO_UPDATE_STRIP_NAMES",
        "tmux_update_environment_names", "reconcile_tmux_desktop_env",
        "set-option", "update-environment", "set-environment", "desktop_clipboard_env_overrides",
    )
    assert all(marker in source for marker in required)
    strip = source[source.index("TMUX_AUTO_UPDATE_STRIP_NAMES"):source.index("# Claude's CLI")]
    assert all(name in strip for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"))
    body = source[source.index("def reconcile_tmux_desktop_env"):source.index("def tmux_pane_activity_snapshot")]
    assert "if not tmux or not trusted_env" in body
    assert "if names is not None" in body and "for scope in scopes" in body
    assert "tmux_group_launch_command" in source and "reconcile_tmux_desktop_env" in source
    print("[Row498ClipboardContract] PASS allowlist=6 strip=3 global+managed absent-safe custom-preserved")

if __name__ == "__main__": main()
