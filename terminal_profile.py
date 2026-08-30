"""GNOME Terminal profile/font/color resolution + exact tmux targeting, shared by session_hub.py
(the Qt GUI process) and vte_embed_helper.py (a separate GTK3/Vte process) (task-2142 row453
REWORK -- orchestrator audit, 2026-08-30). Deliberately dependency-free beyond the stdlib: the
helper process must never import session_hub.py itself, since that would pull in PyQt6 and the
rest of a 9k-line module into a process that only needs a few pure functions, and mixing two GUI
toolkits' native libraries into one process is its own risk. This is the ONE place both the
external gnome-terminal launch path and the embedded VTE helper resolve a profile/font/target
from, so a control test can assert they cannot silently diverge.
"""
from __future__ import annotations

import subprocess


def tmux_exact_target(name: str) -> str:
    """`=<name>` -- tmux's exact-match session target syntax, so a stale name that is a PREFIX of
    another live session can never attach to the wrong one (same contract as session_hub.py's own
    `tmux_exact_target`, row447 rework; duplicated here rather than imported so this module stays
    import-cheap and PyQt6-free for the helper process)."""
    return f"={name}"


def _gsettings_get(schema: str, key: str, run=subprocess.run) -> str | None:
    """One gsettings read, or None on any failure (missing binary, wrong schema/path, no
    desktop) -- the single low-level primitive every profile/font resolver below goes through, so
    a hermetic test can fake `run` once and cover all of them."""
    try:
        out = run(["gsettings", "get", schema, key], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip().strip("'")
    return value or None


def resolve_gnome_terminal_profile_uuid(run=subprocess.run) -> str | None:
    """The GNOME Terminal profile uuid Session Hub's EXTERNAL gnome-terminal launch path
    implicitly uses (the desktop default profile). This is the ONE place both the external
    launch and the embedded VTE helper resolve a profile from, so a control test can assert they
    cannot silently diverge."""
    return _gsettings_get("org.gnome.Terminal.ProfilesList", "default", run)


def _profile_get(profile_uuid: str, key: str, run=subprocess.run) -> str | None:
    schema = "org.gnome.Terminal.Legacy.Profile"
    path = f"/org/gnome/terminal/legacy/profiles:/:{profile_uuid}/"
    return _gsettings_get(f"{schema}:{path}", key, run)


def resolve_gnome_terminal_font(profile_uuid: str | None, run=subprocess.run) -> str | None:
    """The font gnome-terminal actually renders with for `profile_uuid`: the profile's own
    `font` key when it opted out of the system font, else GNOME's desktop-wide monospace font
    (`use-system-font=true` is the common case -- a profile with no font override has no `font`
    key to read at all, so falling back to the profile alone silently produces no font, not the
    live default)."""
    if profile_uuid and _profile_get(profile_uuid, "use-system-font", run) != "true":
        font = _profile_get(profile_uuid, "font", run)
        if font:
            return font
    return _gsettings_get("org.gnome.desktop.interface", "monospace-font-name", run)


def gnome_terminal_profile_style(profile_uuid: str | None, run=subprocess.run) -> dict:
    """font/background/foreground/cursor_shape to make an embedded terminal match GNOME
    Terminal's rendering of `profile_uuid`. Colors are omitted (letting the embedded GTK app's
    own theme apply, exactly as gnome-terminal's do) whenever the profile has
    `use-theme-colors=true` -- the common case -- since hardcoding a color pair there would fight
    the theme gnome-terminal itself is actually using."""
    style: dict[str, str] = {}
    font = resolve_gnome_terminal_font(profile_uuid, run)
    if font:
        style["font"] = font
    if profile_uuid:
        if _profile_get(profile_uuid, "use-theme-colors", run) != "true":
            bg = _profile_get(profile_uuid, "background-color", run)
            fg = _profile_get(profile_uuid, "foreground-color", run)
            if bg:
                style["background"] = bg
            if fg:
                style["foreground"] = fg
        cursor_shape = _profile_get(profile_uuid, "cursor-shape", run)
        if cursor_shape:
            style["cursor_shape"] = cursor_shape
    return style
