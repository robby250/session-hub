#!/usr/bin/env bash
# Populate a second Claude Code account config dir (CLAUDE_CONFIG_DIR target)
# by symlinking everything from the source ~/.claude except a fixed isolate
# list, which gets created fresh/empty instead. Rerunnable: only fills in
# entries missing from the target, never touches ones already there.
set -euo pipefail

SOURCE="${SOURCE:-$HOME/.claude}"
TARGET="${1:?usage: setup_claude_account.sh <target-config-dir>}"

ISOLATE=(
    .credentials.json
    stats-cache.json
    daemon
    daemon.lock
    daemon.status.json
    daemon.log
    sessions
    mcp-needs-auth-cache.json
    .last-cleanup
    .last-update-result.json
    history.jsonl
)

is_isolated() {
    local name="$1"
    for entry in "${ISOLATE[@]}"; do
        [[ "$name" == "$entry" ]] && return 0
    done
    return 1
}

mkdir -p "$TARGET"

for src_path in "$SOURCE"/* "$SOURCE"/.[!.]*; do
    [[ -e "$src_path" || -L "$src_path" ]] || continue
    name="$(basename "$src_path")"
    target_path="$TARGET/$name"
    [[ -e "$target_path" || -L "$target_path" ]] && continue

    if is_isolated "$name"; then
        if [[ -d "$src_path" && ! -L "$src_path" ]]; then
            mkdir -p "$target_path"
        elif [[ "$name" == *.json ]]; then
            echo "{}" > "$target_path"
        else
            : > "$target_path"
        fi
        echo "fresh    $target_path"
    else
        ln -s "$src_path" "$target_path"
        echo "symlink  $target_path -> $src_path"
    fi
done
