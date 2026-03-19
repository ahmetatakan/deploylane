from __future__ import annotations

import typer

from ..config import (
    Profile,
    load_config,
    get_active_profile_name,
    get_profile,
)


def _err(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def get_active_profile_or_exit() -> Profile:
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if not prof:
        _err("Not logged in. Run: dlane login")
    return prof


def get_workspace_profile_or_exit(ws) -> Profile:
    """Load the profile defined in workspace.yml, falling back to active profile."""
    cfg = load_config()
    ws_profile_name = getattr(ws, "default_profile", None) or "default"
    prof = get_profile(cfg, ws_profile_name)
    if prof:
        active = get_active_profile_name(cfg)
        if active != ws_profile_name:
            typer.secho(
                f"  (using workspace profile '{ws_profile_name}', active is '{active}')",
                fg=typer.colors.YELLOW,
            )
        return prof
    # fallback to active profile
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if not prof:
        _err(
            f"Profile '{ws_profile_name}' not found and no active profile.\n"
            f"  Run: dlane login --profile {ws_profile_name}"
        )
    typer.secho(
        f"  (workspace profile '{ws_profile_name}' not found, using active '{active}')",
        fg=typer.colors.YELLOW,
    )
    return prof
