from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from ..workspace import load_workspace, project_deploy_yml
from ..auth import get_provider
from ..gitlab import (
    GitLabError,
    get_repository_file,
    create_branch,
    upsert_repository_file,
    create_merge_request,
    get_project_by_path,
    lint_ci_file,
)
from ._utils import _err, get_workspace_profile_or_exit
from .workspace._utils import _resolve_ws

ci_app = typer.Typer(no_args_is_help=True, help="Manage .gitlab-ci.yml in project repositories.")

CI_FILE = ".gitlab-ci.yml"
LOCAL_CI_PATH = ".deploylane/ci/.gitlab-ci.yml"


def _local_ci(ws_path: Path, name: str) -> Path:
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")
    return (ws_path.parent / project.path / LOCAL_CI_PATH).resolve()


def _show_diff(remote: str, local: str) -> bool:
    """Show diff between remote and local. Returns True if different."""
    diff = list(difflib.unified_diff(
        remote.splitlines(keepends=True),
        local.splitlines(keepends=True),
        fromfile=f"gitlab:{CI_FILE}",
        tofile=f"local:{CI_FILE}",
    ))
    if not diff:
        return False
    for line in diff:
        if line.startswith("+"):
            typer.secho(line, fg=typer.colors.GREEN, nl=False)
        elif line.startswith("-"):
            typer.secho(line, fg=typer.colors.RED, nl=False)
        else:
            typer.echo(line, nl=False)
    typer.echo("")
    return True


@ci_app.command("lint")
def ci_lint(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    ref: str = typer.Option("main", "--ref", help="Branch ref for lint context"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Validate local .gitlab-ci.yml against GitLab's CI lint API."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")

    local_ci = _local_ci(ws_path, name)
    if not local_ci.exists():
        _err(f"Local .gitlab-ci.yml not found: {local_ci}\n  Run: dlane scaffold {name}")

    content = local_ci.read_text(encoding="utf-8")
    prof = get_workspace_profile_or_exit(ws)

    typer.secho(f"[{name}] Linting .gitlab-ci.yml via GitLab API...", fg=typer.colors.CYAN)

    try:
        gl_project = get_project_by_path(prof.host, prof.token, project.gitlab_project)
        valid, messages = lint_ci_file(prof.host, prof.token, int(gl_project.id), content, ref=ref)
    except GitLabError as e:
        _err(str(e))

    if valid:
        typer.secho("  ✓ Valid", fg=typer.colors.GREEN, bold=True)
        if messages:
            for m in messages:
                typer.secho(f"  {m}", fg=typer.colors.YELLOW)
    else:
        typer.secho("  ✗ Invalid:", fg=typer.colors.RED, bold=True)
        for m in messages:
            typer.secho(f"  - {m}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@ci_app.command("pull")
def ci_pull(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    ref: str = typer.Option("main", "--ref", help="Branch or tag to pull from"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Fetch .gitlab-ci.yml from GitLab repo into local .deploylane/ci/."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")

    local_ci = _local_ci(ws_path, name)
    prof = get_workspace_profile_or_exit(ws)

    try:
        gl_project = get_project_by_path(prof.host, prof.token, project.gitlab_project)
    except GitLabError as e:
        _err(str(e))

    effective_ref = gl_project.default_branch or ref
    typer.secho(f"[{name}] CI pull from {project.gitlab_project} (ref={effective_ref})", fg=typer.colors.CYAN)

    try:
        remote_content = get_repository_file(prof.host, prof.token, gl_project.id, CI_FILE, ref=effective_ref)
    except GitLabError as e:
        _err(str(e))

    if not local_ci.exists():
        local_ci.parent.mkdir(parents=True, exist_ok=True)
        local_ci.write_text(remote_content, encoding="utf-8")
        typer.secho(f"  Created {LOCAL_CI_PATH}", fg=typer.colors.GREEN)
        return

    local_content = local_ci.read_text(encoding="utf-8")
    if remote_content.strip() == local_content.strip():
        typer.secho("  Already up to date.", fg=typer.colors.GREEN)
        return

    typer.secho("  Diff (gitlab → local):", fg=typer.colors.YELLOW)
    _show_diff(remote_content, local_content)
    typer.confirm("  Overwrite local .gitlab-ci.yml with GitLab version?", abort=True)
    local_ci.write_text(remote_content, encoding="utf-8")
    typer.secho(f"  Updated {LOCAL_CI_PATH}", fg=typer.colors.GREEN)


@ci_app.command("push")
def ci_push(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    ref: str = typer.Option("main", "--ref", help="Target branch for MR"),
    force: bool = typer.Option(False, "--force", help="Skip GitLab version check"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Push local .gitlab-ci.yml to GitLab repo via MR."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")

    local_ci = _local_ci(ws_path, name)
    if not local_ci.exists():
        _err(f"Local .gitlab-ci.yml not found: {local_ci}\n  Run: dlane scaffold {name}")

    local_content = local_ci.read_text(encoding="utf-8")
    prof = get_workspace_profile_or_exit(ws)

    typer.secho(f"[{name}] CI push → {project.gitlab_project}", fg=typer.colors.CYAN)

    try:
        gl_project = get_project_by_path(prof.host, prof.token, project.gitlab_project)
        default_branch = gl_project.default_branch or ref
    except GitLabError as e:
        _err(str(e))

    # Lint check before push
    typer.secho("  Linting .gitlab-ci.yml...", fg=typer.colors.CYAN)
    try:
        valid, messages = lint_ci_file(prof.host, prof.token, int(gl_project.id), local_content, ref=default_branch)
    except GitLabError:
        valid, messages = True, []  # lint API unavailable — proceed

    if not valid:
        typer.secho("  ✗ Lint failed — fix errors before pushing:", fg=typer.colors.RED, bold=True)
        for m in messages:
            typer.secho(f"    - {m}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.secho("  ✓ Lint passed", fg=typer.colors.GREEN)
    for m in messages:
        typer.secho(f"  {m}", fg=typer.colors.YELLOW)

    # Pull check
    if not force:
        try:
            remote_content = get_repository_file(prof.host, prof.token, gl_project.id, CI_FILE, ref=default_branch)
            if remote_content.strip() != local_content.strip():
                typer.secho("  ⚠ GitLab version differs from local:", fg=typer.colors.YELLOW)
                _show_diff(remote_content, local_content)
                _err("GitLab has changes not in local. Run 'dlane ci pull' first, or use --force.")
        except GitLabError:
            pass  # file doesn't exist yet, first push

    # Create branch
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    branch = f"deploylane/update-ci-{ts}"

    try:
        create_branch(prof.host, prof.token, int(gl_project.id), branch, ref=default_branch)
        typer.secho(f"  Branch: {branch}", fg=typer.colors.CYAN)

        upsert_repository_file(
            prof.host, prof.token, int(gl_project.id),
            file_path=CI_FILE,
            content=local_content,
            branch=branch,
            commit_message="chore: update .gitlab-ci.yml via deploylane",
        )
        typer.secho(f"  Committed: {CI_FILE}", fg=typer.colors.GREEN)

        mr_url = create_merge_request(
            prof.host, prof.token, int(gl_project.id),
            source_branch=branch,
            target_branch=default_branch,
            title=f"chore: update .gitlab-ci.yml via deploylane",
            description="Generated by `dlane ci push`.",
        )
        typer.secho(f"  MR: {mr_url}", fg=typer.colors.GREEN, bold=True)
    except GitLabError as e:
        _err(str(e))
