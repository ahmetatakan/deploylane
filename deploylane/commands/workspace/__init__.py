from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

from ...stacks import VALID_STRATEGIES, is_valid_strategy
from ...workspace import (
    WorkspaceProject,
    WorkspaceFile,
    load_workspace,
    save_workspace,
    get_project_status,
    WORKSPACE_DIR_NAME,
    WORKSPACE_FILE_NAME,
)
from .._utils import _err
from .init import project_init, workspace_scaffold, _run_init_in_dir
from ._utils import _ws_file_option, _resolve_ws


def workspace_init(
    file: Optional[Path] = typer.Option(None, "--file", help="Path to workspace.yml (default: .workspace/workspace.yml in cwd)"),
):
    """Create .workspace/ directory and workspace.yml in the current directory."""
    ws_path = file or Path(WORKSPACE_FILE_NAME)
    if ws_path.exists():
        _err(f"{ws_path} already exists. Use 'dlane add' to add projects.")
    Path(WORKSPACE_DIR_NAME).mkdir(exist_ok=True)
    ws = WorkspaceFile(version=1, projects=[], default_profile="default")
    save_workspace(ws, ws_path)
    typer.secho(f"Workspace created at {ws_path}", fg=typer.colors.GREEN)

    # Check login status and hint if not logged in
    try:
        from ...auth import load_active_profile
        prof = load_active_profile()
        typer.secho(f"Logged in as: {prof.host}", fg=typer.colors.CYAN)
    except Exception:
        typer.echo("")
        typer.secho("Not logged in yet.", fg=typer.colors.YELLOW)
        typer.echo("  Run: dlane login --host https://gitlab.example.com")

    typer.echo("")
    typer.secho("Getting started:", bold=True)
    typer.echo("  1) dlane add <name> --gitlab-project <group/project> --strategy bluegreen")
    typer.echo("  2) dlane scaffold <name>")
    typer.echo("  3) dlane vars apply <name>")
    typer.echo("  4) dlane deploy push <name> --yes")
    typer.echo("  5) dlane deploy install <name> --yes  (once per server)")
    typer.echo("")
    typer.echo("  Run 'dlane --help' to see all commands.")


def workspace_add(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Short project alias (unique)"),
    gitlab_project: Optional[str] = typer.Option(None, "--gitlab-project", help="GitLab path_with_namespace (e.g. acme/backend)"),
    strategy: Optional[str] = typer.Option(None, "--strategy", help=f"Deployment strategy: {', '.join(VALID_STRATEGIES)}"),
    path: Optional[str] = typer.Option(None, "--path", help="Custom path (default: .workspace/<name>/)"),
    description: str = typer.Option("", "--description", help="Optional description"),
    tag: Optional[List[str]] = typer.Option(None, "--tag", help="Tag (repeatable)"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
    init: bool = typer.Option(False, "--init", help="Scaffold .deploylane/ in the project directory after adding"),
):
    """Add a project to the workspace. Creates .workspace/<name>/ by default."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    if not gitlab_project:
        _err(f"Missing required option: --gitlab-project\n  Try: dlane add {name} --gitlab-project <group/project> --strategy <strategy>")
    if not strategy:
        strategy = "plain"
    if not is_valid_strategy(strategy):
        _err(f"Unknown strategy '{strategy}'. Valid: {', '.join(VALID_STRATEGIES)}")

    ws_path = file or _ws_file_option()
    if ws_path.exists():
        ws = load_workspace(ws_path)
    else:
        ws = WorkspaceFile(version=1, projects=[], default_profile="default")

    if any(p.name == name for p in ws.projects):
        _err(f"Project '{name}' already exists in workspace. Use 'dlane update' to modify it.")

    resolved_path = path or name
    project_path = (ws_path.parent / resolved_path).resolve()

    if not project_path.exists():
        project_path.mkdir(parents=True, exist_ok=True)
        typer.secho(f"  Created {project_path}", fg=typer.colors.GREEN)

    ws.projects.append(WorkspaceProject(
        name=name,
        path=resolved_path,
        gitlab_project=gitlab_project,
        strategy=strategy,
        description=description,
        tags=list(tag) if tag else [],
    ))
    save_workspace(ws, ws_path)
    typer.secho(f"Added '{name}' ({strategy}) to {ws_path}", fg=typer.colors.GREEN)

    if init:
        _run_init_in_dir(project_path, strategy=strategy, project=gitlab_project, ws_name=name)
    else:
        typer.echo(f"  Next: dlane scaffold {name}")


def workspace_update(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias to update"),
    path: Optional[str] = typer.Option(None, "--path", help="New path"),
    gitlab_project: Optional[str] = typer.Option(None, "--gitlab-project", help="New GitLab path_with_namespace"),
    strategy: Optional[str] = typer.Option(None, "--strategy", help=f"New strategy: {', '.join(VALID_STRATEGIES)}"),
    description: Optional[str] = typer.Option(None, "--description", help="New description"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Update an existing project's fields in the workspace."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    if not any([path, gitlab_project, strategy, description]):
        _err(f"Nothing to update. Provide at least one option.\n  Try: dlane update {name} --strategy <strategy>  (or --gitlab-project, --path, --description)")
    ws_path = file or _ws_file_option()
    try:
        ws = load_workspace(ws_path)
    except FileNotFoundError:
        _err(f"workspace.yml not found: {ws_path}")

    match = next((p for p in ws.projects if p.name == name), None)
    if not match:
        _err(f"Project '{name}' not found in workspace.")

    if strategy and not is_valid_strategy(strategy):
        _err(f"Unknown strategy '{strategy}'. Valid: {', '.join(VALID_STRATEGIES)}")

    idx = ws.projects.index(match)
    ws.projects[idx] = WorkspaceProject(
        name=match.name,
        path=path if path is not None else match.path,
        gitlab_project=gitlab_project if gitlab_project is not None else match.gitlab_project,
        strategy=strategy if strategy is not None else match.strategy,
        description=description if description is not None else match.description,
        tags=match.tags,
    )
    save_workspace(ws, ws_path)
    typer.secho(f"Updated '{name}' in {ws_path}", fg=typer.colors.GREEN)


def workspace_remove(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Remove a project from the workspace."""
    if not name:
        _err("Provide a project name.\n  Try: dlane remove <name>\n  Use 'dlane list' to see projects.")
    ws_path = file or _ws_file_option()
    try:
        ws = load_workspace(ws_path)
    except FileNotFoundError:
        _err(f"workspace.yml not found: {ws_path}")

    match = next((p for p in ws.projects if p.name == name), None)
    if not match:
        _err(f"Project '{name}' not found in workspace.")

    if not yes:
        typer.confirm(f"Remove '{name}' ({match.gitlab_project}) from workspace?", abort=True)

    ws.projects = [p for p in ws.projects if p.name != name]
    save_workspace(ws, ws_path)
    typer.secho(f"Removed '{name}' from {ws_path}", fg=typer.colors.GREEN)


def workspace_list(
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter by tag"),
):
    """List workspace projects with deployment readiness status."""
    ws_path = file or _ws_file_option()
    try:
        ws = load_workspace(ws_path)
    except FileNotFoundError:
        _err("workspace.yml not found. Run: dlane init")

    projects = ws.projects
    if tag:
        projects = [p for p in projects if tag in p.tags]

    if not projects:
        typer.echo("No projects found.")
        return

    ws_dir = ws_path.parent

    rows = []
    for p in sorted(projects, key=lambda x: x.name.lower()):
        st = get_project_status(p, ws_dir)
        rows.append({
            "name":      p.name,
            "strategy":  st["strategy"],
            "gitlab":    p.gitlab_project,
            "deploy_ok": st["deploy_yml_exists"],
            "vars_ok":   st["vars_yml_exists"],
            "targets":   ", ".join(st["targets"]) if st["targets"] else "-",
        })

    w_name     = max(max(len(r["name"])     for r in rows), 4)
    w_strategy = max(max(len(r["strategy"]) for r in rows), 8)
    w_gitlab   = max(max(len(r["gitlab"])   for r in rows), 14)
    w_targets  = max(max(len(r["targets"])  for r in rows), 7)

    SEP = "  "

    def _cell(text: str, width: int) -> str:
        return f"{text:<{width}}"

    typer.echo("")
    typer.secho(
        SEP
        + _cell("NAME",           w_name)     + SEP
        + _cell("STRATEGY",       w_strategy) + SEP
        + _cell("GITLAB PROJECT", w_gitlab)   + SEP
        + _cell("DEPLOY", 6)                  + SEP
        + _cell("VARS",   4)                  + SEP
        + "TARGETS",
        bold=True,
    )
    typer.echo(
        SEP
        + "─" * w_name     + SEP
        + "─" * w_strategy + SEP
        + "─" * w_gitlab   + SEP
        + "─" * 6          + SEP
        + "─" * 4          + SEP
        + "─" * w_targets
    )

    for r in rows:
        d_icon = typer.style("✓", fg=typer.colors.GREEN) if r["deploy_ok"] else typer.style("✗", fg=typer.colors.RED)
        v_icon = typer.style("✓", fg=typer.colors.GREEN) if r["vars_ok"]   else typer.style("✗", fg=typer.colors.YELLOW)

        typer.echo(
            SEP
            + _cell(r["name"],     w_name)     + SEP
            + _cell(r["strategy"], w_strategy) + SEP
            + _cell(r["gitlab"],   w_gitlab)   + SEP
            + "  " + d_icon + "   "            + SEP
            + " "  + v_icon + "  "             + SEP
            + r["targets"]
        )

    typer.echo("")


__all__ = [
    "workspace_init",
    "workspace_add",
    "workspace_update",
    "workspace_remove",
    "workspace_list",
    "workspace_scaffold",
    "project_init",
]
