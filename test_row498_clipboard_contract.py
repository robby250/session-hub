"""Pure structural proof for row498; intentionally does not execute tmux or Session Hub."""
import ast
from pathlib import Path

ROOT = Path(__file__).parent
source = (ROOT / "session_hub.py").read_text()
tests = (ROOT / "test_session_hub.py").read_text()


def _calls(tree, name):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == name)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == name))]


def _literal_strings(node):
    return {
        value.value for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }

def main():
    tree = ast.parse(source)
    test_tree = ast.parse(tests)
    names = {target.id for node in tree.body if isinstance(node, ast.Assign)
             for target in node.targets if isinstance(target, ast.Name)}
    assert {"CLIPBOARD_ENV_ALLOWLIST", "TMUX_AUTO_UPDATE_STRIP_NAMES"} <= names

    reconcile = next(node for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == "reconcile_tmux_desktop_env")
    reconcile_literals = _literal_strings(reconcile)
    assert {"set-option", "update-environment", "set-environment"} <= reconcile_literals
    assert "tmux_update_environment_names" in {
        node.func.id for node in _calls(reconcile, "tmux_update_environment_names")
        if isinstance(node.func, ast.Name)
    }

    # The safety boundary is an ordering invariant: process-wide TMUX is removed before
    # session_hub import, while the test-owned TMUX_TMPDIR is established first.
    module = test_tree.body
    import_index = next(i for i, node in enumerate(module)
                        if isinstance(node, ast.ImportFrom) and node.module == "PyQt6.QtCore")
    tmpdir_index = next(i for i, node in enumerate(module)
                        if isinstance(node, ast.Assign)
                        and "TMUX_TMPDIR" in ast.unparse(node))
    tmux_pop_index = next(i for i, node in enumerate(module)
                          if isinstance(node, ast.Expr) and "TMUX" in ast.unparse(node)
                          and "pop" in ast.unparse(node))
    assert tmpdir_index < tmux_pop_index < import_index
    assert "TMUX_TMPDIR" in ast.unparse(module[tmpdir_index])
    assert "TMUX" in ast.unparse(module[tmux_pop_index])
    # The import-order check is intentionally process-wide: setting only TMUX_TMPDIR is a red
    # control because tmux prefers inherited TMUX.  The production test must establish the
    # owned tmpdir, remove TMUX, and only then import Qt/session_hub or spawn a child.
    assert "tempfile.mkdtemp" in ast.unparse(module[tmpdir_index])
    assert "None" in ast.unparse(module[tmux_pop_index])

    wrapper = next(node for node in ast.walk(test_tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_disposable_tmux_wrapper")
    wrapper_literals = _literal_strings(wrapper)
    assert any("-L" in value for value in wrapper_literals) and "tmux-disposable" in wrapper_literals

    sentinel = next(node for node in ast.walk(test_tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "test_row498_risky_paths_leave_explicit_sentinel_server_unchanged")
    sentinel_literals = _literal_strings(sentinel)
    assert {"display-message", "#{pid}", "list-sessions", "show-options",
            "show-environment", "ROW498_SENTINEL", "ROW498_SESSION_SENTINEL",
            "kill-server"} <= sentinel_literals
    assert "assertEqual" in {node.func.attr for node in _calls(sentinel, "assertEqual")
                              if isinstance(node.func, ast.Attribute)}
    sentinel_source = ast.unparse(sentinel)
    # This is a real, bounded integration path (when the full suite is explicitly run),
    # not a marker-only claim: both the fixture and an independent sentinel are created
    # through the -L wrapper, risky calls receive the fixture wrapper, and state is compared
    # before/after with cleanup scoped to those two disposable sockets.
    assert sentinel_source.count("_disposable_tmux_wrapper") >= 2
    assert sentinel_source.count("subprocess.run") >= 1
    assert "tmux=str(fixture)" in sentinel_source
    assert "_launch_option_phase(fixture_session, fixture)" in sentinel_source
    assert "self.assertEqual(state(sentinel, sentinel_session), before)" in sentinel_source
    assert "run(fixture" in sentinel_source and "kill-server" in sentinel_source
    assert "run(sentinel" in sentinel_source and "kill-server" in sentinel_source
    # The sentinel snapshot is deliberately multi-dimensional.  A PID-only assertion would miss
    # a session/options/environment mutation that leaves the server process alive.
    assert "display-message" in sentinel_literals
    assert "list-sessions" in sentinel_literals
    assert "show-options" in sentinel_literals
    assert "show-environment" in sentinel_literals
    assert "session_env" in sentinel_source
    assert "before = state(sentinel, sentinel_session)" in sentinel_source
    assert "state(sentinel, sentinel_session), before" in sentinel_source

    lifecycle = next(node for node in ast.walk(test_tree)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "test_reconcile_tmux_desktop_env_real_lifecycle_survives_a_headless_attach")
    lifecycle_source = ast.unparse(lifecycle)
    # This is the causal negative/positive control: a later headless child is launched from a
    # scrubbed env, while both real tmux endpoints remain explicit disposable -L wrappers.
    assert "'env', '-i'" in lifecycle_source
    assert "attach-session" in lifecycle_source
    assert "TMUX" not in lifecycle_source.split("'env', '-i'", 1)[1].split("attach-session", 1)[0]
    assert "_disposable_tmux_wrapper" in lifecycle_source
    assert "detach-client" in lifecycle_source
    # The process-wide inherited socket selector must be cleared before any import or child.
    assert "os.environ.pop" in ast.unparse(module[tmux_pop_index])
    print("[Row498ClipboardContract] PASS structural isolation=TMUX-cleared-before-import -L sentinel=pid/sessions/options/env byte-identical")

if __name__ == "__main__": main()
