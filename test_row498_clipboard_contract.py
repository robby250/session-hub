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
    print("[Row498ClipboardContract] PASS structural isolation=TMUX-cleared-before-import -L sentinel=pid/sessions/options/env byte-identical")

if __name__ == "__main__": main()
