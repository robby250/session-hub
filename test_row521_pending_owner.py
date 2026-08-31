"""Pure row521 resolver controls; no tmux, Session Hub, GUI, or subprocess calls."""
import ast
from pathlib import Path

def resolve(row, live_by_name):
    if row.get("provider") != "Codex" or not row.get("codex_pending_since"): return None
    owner = live_by_name.get(row["name"])
    if owner is None or owner["updated_ms"] < row["codex_pending_since"]: return None
    return owner["key"]


def resolve_from_census(row, live_by_key, owner_by_key):
    """Small pure model of the production batched-census path."""
    if row.get("provider") != "Codex" or not row.get("codex_pending_since"):
        return None
    native_key = next(
        (key for key, name in owner_by_key.items() if name == row.get("name")),
        None,
    )
    owner = live_by_key.get(native_key)
    if owner is None or owner["updated_ms"] < row["codex_pending_since"]:
        return None
    return owner["key"]

def main():
    worker = {"name": "VAMP-worker4", "provider": "Codex", "codex_pending_since": 100}
    orch = {"name": "VAMP-orchestrator", "provider": "Codex", "session_key": "Codex:orch"}
    newer_orch = {"key": "Codex:orch", "updated_ms": 200}
    assert resolve(worker, {}) is None
    assert resolve(worker, {"VAMP-orchestrator": newer_orch}) is None
    assert resolve(worker, {"VAMP-worker4": {"key": "Codex:w4", "updated_ms": 101}}) == "Codex:w4"
    assert resolve(worker, {"VAMP-worker4": {"key": "Codex:w4", "updated_ms": 99}}) is None
    census = {"Codex:w4": "VAMP-worker4", "Codex:orch": "VAMP-orchestrator"}
    live_by_key = {
        "Codex:w4": {"key": "Codex:w4", "updated_ms": 101},
        "Codex:orch": {"key": "Codex:orch", "updated_ms": 200},
    }
    assert resolve_from_census(worker, live_by_key, census) == "Codex:w4"
    assert resolve_from_census(worker, live_by_key, {"Codex:orch": "VAMP-orchestrator"}) is None
    assert resolve_from_census(worker, live_by_key, {"Codex:w4": "VAMP-worker4"}) == "Codex:w4"
    # Metadata order reversal cannot alter exact-name ownership.
    assert [resolve(row, {"VAMP-worker4": {"key": "Codex:w4", "updated_ms": 101}})
            for row in (worker, orch)] == ["Codex:w4", None]
    source = Path(__file__).with_name("session_hub.py").read_text()
    fn = source[source.index("def pending_codex_exact_owner"):source.index("def discover_sessions")]
    code = "\n".join(line for line in fn.splitlines() if not line.lstrip().startswith(('"', "#")))
    for forbidden in ("find_group_member_session", "Path(row.get(\"cwd\"", "updated transcript"):
        if forbidden in code:
            raise AssertionError("resolver regressed to non-authoritative identity: " + forbidden)
    assert "codex_tmux_native_key" in fn and "session_key" not in fn
    tree = ast.parse(source)
    funcs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def call_lines(function, callee):
        return [node.lineno for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and ((isinstance(node.func, ast.Name) and node.func.id == callee)
                     or (isinstance(node.func, ast.Attribute) and node.func.attr == callee))]

    # Every direct group-row consumer either invokes repair itself with its shared census
    # or routes through group_row_candidates, which does the same before matching.
    for consumer in ("discover_sessions", "group_row_candidates", "refresh_running_tab", "sessions_json_cli"):
        assert call_lines(funcs[consumer], "resolve_pending_codex_group_rows"), consumer
    discover_body = funcs["discover_sessions"]
    assert call_lines(discover_body, "compute_codex_tmux_owner_census")
    assert any(
        any(keyword.arg == "tmux_owner_by_native_key" for keyword in node.keywords)
        for node in ast.walk(discover_body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_pending_codex_group_rows"
    )
    assert not call_lines(discover_body, "codex_tmux_native_key")
    assert "tmux_owner_by_native_key" in {
        arg.arg for arg in funcs["group_row_candidates"].args.kwonlyargs
    } or "tmux_owner_by_native_key" in {
        arg.arg for arg in funcs["group_row_candidates"].args.args
    }
    for consumer in ("refresh_running_tab", "sessions_json_cli"):
        body = funcs[consumer]
        assert call_lines(body, "compute_codex_tmux_owner_census")
        assert any(
            any(keyword.arg == "tmux_owner_by_native_key" for keyword in node.keywords)
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_pending_codex_group_rows"
        )

    # A multi-row discovery pass receives one shared census map; the one-name
    # subprocess lookup remains available only to explicitly map-less direct callers.
    assert "tmux_owner_by_native_key=tmux_owner_by_native_key" in ast.unparse(discover_body)
    assert "if tmux_owner_by_native_key is None" in ast.unparse(discover_body)

    # Launch must repair before it clears a pending marker; otherwise an exact owner is
    # converted into a fresh duplicate before the shared resolver can see it.
    launch = funcs["launch_group_row"]
    candidate_lines = call_lines(launch, "group_row_candidates")
    pop_lines = [node.lineno for node in ast.walk(launch)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "pop"
                 and any(isinstance(arg, ast.Constant) and arg.value == "codex_pending_since"
                         for arg in node.args)]
    assert candidate_lines and pop_lines and min(candidate_lines) < min(pop_lines)
    assert call_lines(funcs["resume_group_row"], "group_row_candidates")

    print("[Row521PendingOwner] PASS exact-owner pending-unmatched same-cwd-refused repair-shared-before-actions order-independent")

if __name__ == "__main__": main()
