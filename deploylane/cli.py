from __future__ import annotations

from typing import Optional
import typer

from . import __version__

from .config import (
    load_config,
    get_active_profile_name,
    get_profile,
    env_fallback_host,
    env_fallback_token,
    normalize_host,
)
from .commands.auth import config_app, profile_app, login, logout, whoami
from .commands.projects import project_app
from .commands.vars import vars_app
from .commands.deploy import deploy_app
from .commands.workspace import (
    workspace_init,
    workspace_add,
    workspace_update,
    workspace_remove,
    workspace_list,
    workspace_scaffold,
)

app = typer.Typer(add_completion=True, no_args_is_help=True)

# ─── Auth ─────────────────────────────────────────────────────────────────────

app.command(rich_help_panel="Auth")(login)
app.command(rich_help_panel="Auth")(logout)
app.command(rich_help_panel="Auth")(whoami)
app.add_typer(profile_app, name="profile", rich_help_panel="Auth")

# ─── Workspace ────────────────────────────────────────────────────────────────

app.command("init",   rich_help_panel="Workspace")(workspace_init)
app.command("add",    rich_help_panel="Workspace")(workspace_add)
app.command("update", rich_help_panel="Workspace")(workspace_update)
app.command("remove", rich_help_panel="Workspace")(workspace_remove)
app.command("list",     rich_help_panel="Workspace")(workspace_list)
app.command("scaffold", rich_help_panel="Workspace")(workspace_scaffold)

# ─── Operations ───────────────────────────────────────────────────────────────

app.add_typer(vars_app,   name="vars",   rich_help_panel="Operations")
app.add_typer(deploy_app, name="deploy", rich_help_panel="Operations")

# ─── Tools ────────────────────────────────────────────────────────────────────

app.add_typer(config_app,  name="config", rich_help_panel="Tools")
app.add_typer(project_app, name="gitlab", rich_help_panel="Tools")

# ─── Global callback ──────────────────────────────────────────────────────────

@app.callback(invoke_without_command=True)
def _global_callback(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(None, "--version", "-v", is_eager=True, help="Show version and exit."),
) -> None:
    if version:
        typer.echo(f"deploylane {__version__}")
        raise typer.Exit()
    if getattr(ctx, "resilient_parsing", False):
        return

    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)

    source = "config" if prof else "env" if (env_fallback_host() and env_fallback_token()) else "none"
    host = prof.host if prof else (normalize_host(env_fallback_host()) if source == "env" else "-")

    tag = f" ({source})" if source != "config" else ""
    typer.secho(f"▶ profile: {active}{tag} -- host: {host}", fg=typer.colors.GREEN, bold=True)

    if prof is None and source != "env":
        typer.secho("  (not logged in for this profile)", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    app()
