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
