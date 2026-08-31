"""Pure row517 preview contract; no Textual, Qt, tmux, or subprocess is started."""
import ast
from pathlib import Path


def compact_row(row: dict) -> str:
    # Mirrors the data boundary, not the widget: status detail is never a preview fallback.
    preview = str(row.get("assistant_preview", ""))
    return preview[:240]


def main() -> None:
    grouped = {
        "name": "Worker4", "key": "Codex:worker4", "activity_detail": "working",
        "assistant_preview": "the newest assistant answer",
    }
    standalone = {
        "name": "orchestrator", "key": "Codex:orchestrator",
        "activity_detail": "done", "assistant_preview": "standalone answer",
    }
    assert compact_row(grouped) == "the newest assistant answer"
    assert compact_row(standalone) == "standalone answer"
    assert compact_row({"name": "missing", "key": "Codex:missing", "activity_detail": "status"}) == ""
    assert compact_row({"name": "unreadable", "key": "Codex:unreadable", "assistant_preview": ""}) == ""

    tui = ast.parse(Path(__file__).with_name("session_hub_tui.py").read_text())
    running_class = next(node for node in ast.walk(tui)
                         if isinstance(node, ast.ClassDef) and node.name == "RunningPane")
    running = next(node for node in running_class.body
                   if isinstance(node, ast.FunctionDef) and node.name == "apply_sessions")
    source = ast.unparse(running)
    assert "assistant_preview" in source and "activity_detail" not in source
    tui_source = Path(__file__).with_name("session_hub_tui.py").read_text()
    pane_source = tui_source[tui_source.index("class RunningPane"):]
    assert "self.adapter.switch(target)" in pane_source
    assert "host.content_size" in pane_source
    assert "def on_unmount" in pane_source
    assert "return f\"{first}\\n{second}\"" in pane_source
    assert ".running-row { height: 2;" in pane_source

    hub = ast.parse(Path(__file__).with_name("session_hub.py").read_text())
    json_cli = next(node for node in ast.walk(hub)
                    if isinstance(node, ast.FunctionDef) and node.name == "sessions_json_cli")
    calls = [node for node in ast.walk(json_cli)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert any(node.func.id == "serialized_assistant_preview" for node in calls)
    fields = {
        key.value for node in ast.walk(json_cli)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "assistant_preview" in fields
    print("[Row517Preview] PASS grouped+standalone assistant-only missing/unreadable-empty status-not-fallback")


if __name__ == "__main__":
    main()
