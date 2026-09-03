"""Force XDG_DATA_HOME to a disposable directory, at import time.

Import this BEFORE `session_hub` in any test module. `session_hub` derives
METADATA_PATH, PID_DIR, STATUS_DIR, METADATA_BACKUP_DIR and TRASH_DIR from
DATA_DIR at import time, so a module that imports it under the real
XDG_DATA_HOME can reconcile and rewrite the user's live metadata.json as a
side effect -- no test body required.

Why this file exists rather than a documented "run me with a prefix" contract:
13 of the 15 test modules here carried that contract in a docstring, and on
2026-09-03 02:13 one of them (test_row2243_manage_group_activity_identity.py)
was run without the prefix and truncated the live metadata.json from 63
sessions to a 189-byte stub holding its own `tmux-dup-x` fixture. The VAMPULSE
`no_unsandboxed_session_hub` PreToolUse hook does not close this: it is a
Claude Code hook, and the sessions that run these tests are Codex, which never
evaluates it. The import is the only layer both harnesses share.

Only the directory created here is ever removed; a caller-provided
XDG_DATA_HOME is overridden in-process but never touched on disk.
"""

import atexit
import os
import shutil
import tempfile

_TEST_XDG_DATA_HOME = tempfile.mkdtemp(prefix="session-hub-test-xdg-")
os.environ["XDG_DATA_HOME"] = _TEST_XDG_DATA_HOME
atexit.register(shutil.rmtree, _TEST_XDG_DATA_HOME, ignore_errors=True)
