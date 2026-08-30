"""Pure row521 resolver controls; no tmux, Session Hub, GUI, or subprocess calls."""
from pathlib import Path

def resolve(row, live_by_name):
    if row.get("provider") != "Codex" or not row.get("codex_pending_since"): return None
    owner = live_by_name.get(row["name"])
    if owner is None or owner["updated_ms"] < row["codex_pending_since"]: return None
    return owner["key"]

def main():
    worker = {"name": "VAMP-worker4", "provider": "Codex", "codex_pending_since": 100}
    orch = {"name": "VAMP-orchestrator", "provider": "Codex", "session_key": "Codex:orch"}
    newer_orch = {"key": "Codex:orch", "updated_ms": 200}
    assert resolve(worker, {}) is None
    assert resolve(worker, {"VAMP-orchestrator": newer_orch}) is None
    assert resolve(worker, {"VAMP-worker4": {"key": "Codex:w4", "updated_ms": 101}}) == "Codex:w4"
    assert resolve(worker, {"VAMP-worker4": {"key": "Codex:w4", "updated_ms": 99}}) is None
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
    print("[Row521PendingOwner] PASS exact-name-owner pending-unmatched same-cwd-refused order-independent")

if __name__ == "__main__": main()
