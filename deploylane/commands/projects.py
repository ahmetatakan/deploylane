from __future__ import annotations

from typing import Optional

import typer

from ..config import load_config, get_active_profile_name, get_profile
from ..gitlab import list_projects, GitLabError
from ._utils import _err

project_app = typer.Typer(no_args_is_help=True, help="Browse GitLab repositories.")


@project_app.command("list")
def projects_list(
    search: Optional[str] = typer.Option(None, "--search", help="Search projects by name/path"),
    owned: bool = typer.Option(False, "--owned", help="Only projects owned by the user"),
    membership: bool = typer.Option(True, "--membership/--no-membership", help="Only projects the user is a member of"),
    limit: int = typer.Option(50, "--limit", min=1, max=5000, help="Max number of projects to print (after sorting)"),
):
    """List GitLab repositories visible to the active profile."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if prof is None:
        _err(f"Not logged in (active profile '{active}'). Run: dlane login --profile {active}")

    try:
        projects = list_projects(
            host=prof.host,
            token=prof.token,
            search=search,
            owned=owned,
            membership=membership,
        )
    except GitLabError as e:
        _err(str(e))

    if not projects:
        typer.echo("No projects returned.")
        typer.echo("Try: dlane gitlab list --no-membership  (or --owned)")
        raise typer.Exit(code=0)

    projects = sorted(projects, key=lambda p: p.path_with_namespace.lower())
    projects = projects[:limit]

    for p in projects:
        typer.echo(
            f"{p.id}\t{p.path_with_namespace}\t"
            f"{p.default_branch or '-'}\t"
            f"{p.web_url or '-'}"
        )
