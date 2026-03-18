from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import typer

from ..deployspec import load_deployspec
from ..deploylog import read_log
from ..remote import copy_file, RemoteError, ssh_run_interactive
from ..workspace import load_workspace, project_deploy_yml
from ._utils import _err
from .workspace._utils import _resolve_ws

deploy_app = typer.Typer(no_args_is_help=True, help="Deploy workspace projects.")


def _load_project(name: str, ws_path: Path):
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace. Use 'dlane list' to see projects.")
    return project



def _resolve_target(name: str, target: Optional[str], ws_path: Path) -> str:
    """Return target: explicit value or default_target from deploy.yml."""
    if target:
        return target
    import yaml as _yaml
    project = _load_project(name, ws_path)
    deploy_yml = project_deploy_yml(project, ws_path.parent)
    if not deploy_yml.exists():
        _err(f"deploy.yml not found: {deploy_yml}\nRun: dlane scaffold {name}")
    try:
        raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")
    resolved = str(raw.get("default_target") or "").strip()
    if not resolved:
        targets = list((raw.get("targets") or {}).keys())
        resolved = targets[0] if targets else ""
    if not resolved:
        _err(f"No default_target in deploy.yml. Use --target <target>.")
    return resolved



def _parse_env(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE env file into a dict, skipping comments and empty lines."""
    result: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v
    return result


def _merged_env_file(base_env: Path, local_env: Path) -> Path:
    """Merge base + local env files, return path to a temp file with merged content."""
    merged = _parse_env(base_env)
    if local_env.exists():
        merged.update(_parse_env(local_env))
    tmp = Path(tempfile.mktemp(suffix=".env"))
    tmp.write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(merged.items())) + "\n",
        encoding="utf-8",
    )
    return tmp


def _push_project(
    ws_path: Path,
    name: str,
    target: str,
    yes: bool,
) -> Tuple[bool, str]:
    """Push .env + compose + deploy.sh to server. Deploy is handled by CI pipeline."""
    ws_dir = ws_path.parent

    try:
        ws = load_workspace(ws_path)
        project = next((p for p in ws.projects if p.name == name), None)
        if not project:
            return False, f"Project '{name}' not found in workspace."

        deploy_yml = project_deploy_yml(project, ws_dir)
        if not deploy_yml.exists():
            return False, f"deploy.yml not found: {deploy_yml}\nRun: dlane scaffold {name}"

        try:
            spec = load_deployspec(deploy_yml)
        except Exception as e:
            return False, f"Invalid deploy.yml: {e}"

        if target not in spec.targets:
            return False, f"Unknown target '{target}'. Available: {', '.join(sorted(spec.targets.keys()))}"

        t = spec.targets[target]
        strategy = (getattr(t, "strategy", "plain") or "plain").strip()
        dest = f"{t.user}@{t.host}"
        remote_dir = t.deploy_dir
        base = ws_dir / project.path / ".deploylane"

        typer.echo(f"  {'(dry run) ' if not yes else ''}{dest}:{remote_dir}")

        # .env  (merged with .local.env if present)
        env_src = base / "env" / f"{target}.env"
        local_env = base / "env" / f"{target}.local.env"
        if not env_src.exists():
            return False, f".env template not found: {env_src}\nRun: dlane scaffold {name}"
        tmp_env = None
        try:
            if local_env.exists():
                tmp_env = _merged_env_file(env_src, local_env)
                push_src = tmp_env
                typer.echo(f"  .deploylane/env/{target}.env + {target}.local.env → {remote_dir}/.env")
            else:
                push_src = env_src
                typer.echo(f"  .deploylane/env/{target}.env → {remote_dir}/.env")
            copy_file(push_src, dest, f"{remote_dir}/.env", dry_run=(not yes))
        except (RemoteError, Exception) as e:
            return False, f"Push .env failed: {e}"
        finally:
            if tmp_env and tmp_env.exists():
                tmp_env.unlink()

        # deploy.sh
        script_src = base / "scripts" / "deploy.sh"
        if script_src.exists():
            try:
                copy_file(script_src, dest, f"{remote_dir}/deploy.sh", dry_run=(not yes))
                typer.echo(f"  deploy.sh → {remote_dir}/deploy.sh")
            except Exception:
                typer.secho("  Warning: could not push deploy.sh", fg=typer.colors.YELLOW)

        # docker-compose.yml
        compose_src = base / "compose" / f"{strategy}.yml"
        if not compose_src.exists():
            compose_src = base / "compose" / "plain.yml"
        if compose_src.exists():
            try:
                copy_file(compose_src, dest, f"{remote_dir}/docker-compose.yml", dry_run=(not yes))
                typer.echo(f"  .deploylane/compose/{strategy}.yml → {remote_dir}/docker-compose.yml")
            except Exception:
                typer.secho("  Warning: could not push docker-compose.yml", fg=typer.colors.YELLOW)

    except Exception as e:
        return False, str(e)

    return True, ""


# ─── Commands ─────────────────────────────────────────────────────────────────



@deploy_app.command("push")
def deploy_push(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias (omit with --all or --tag)"),
    target: Optional[str] = typer.Option(None, "--target", help="Deploy target name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually push (default: dry run)"),
    all_projects: bool = typer.Option(False, "--all", help="Push all projects in workspace"),
    tag: Optional[str] = typer.Option(None, "--tag", help="Push all projects with this workspace tag"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Push .env + docker-compose.yml + deploy.sh to the target server."""
    ws_path = _resolve_ws(file)

    if all_projects or tag:
        ws = load_workspace(ws_path)
        projects: List = ws.projects
        if tag:
            projects = [p for p in projects if tag in (p.tags or [])]
        if not projects:
            label = f"tag={tag}" if tag else "workspace"
            _err(f"No projects found for {label}.")
        names = [p.name for p in sorted(projects, key=lambda p: p.name)]
    elif name:
        names = [name]
    else:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    if len(names) == 1:
        resolved_target = _resolve_target(names[0], target, ws_path)
        typer.secho(f"[{names[0]}] Push {'(dry run)' if not yes else ''} → target={resolved_target}", fg=typer.colors.CYAN)
        ok, err = _push_project(ws_path, names[0], resolved_target, yes)
        if ok:
            typer.secho(f"[{names[0]}] Push OK", fg=typer.colors.GREEN)
        else:
            _err(err)
        return

    label = f"--tag {tag}" if tag else "--all"
    typer.secho(f"▶ Push {len(names)} projects ({label})  {'DRY-RUN' if not yes else 'LIVE'}", fg=typer.colors.CYAN, bold=True)
    typer.echo("")

    results: List[Tuple[str, bool, str]] = []
    for project_name in names:
        resolved_target = _resolve_target(project_name, target, ws_path)
        typer.secho(f"── {project_name}  target={resolved_target} ──────────────────────────", bold=True)
        ok, err = _push_project(ws_path, project_name, resolved_target, yes)
        results.append((project_name, ok, err))
        typer.secho(f"  {'✓ OK' if ok else f'✗ {err}'}", fg=typer.colors.GREEN if ok else typer.colors.RED)
        typer.echo("")

    succeeded = [r for r in results if r[1]]
    failed    = [r for r in results if not r[1]]
    typer.secho("── Summary ──────────────────────────────────", bold=True)
    typer.secho(f"  OK     : {len(succeeded)}/{len(results)}", fg=typer.colors.GREEN if not failed else typer.colors.YELLOW)
    if failed:
        typer.secho(f"  Failed : {len(failed)}", fg=typer.colors.RED)
        for name_, _, err in failed:
            typer.echo(f"    - {name_}: {err}")
        raise typer.Exit(code=1)


@deploy_app.command("install")
def deploy_install(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    target: Optional[str] = typer.Option(None, "--target", help="Deploy target name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually push + run install.sh (default: dry run)"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Push nginx/sudoers/install.sh to server and run install.sh (sudo password prompt shown)."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    target = _resolve_target(name, target, ws_path)
    project = _load_project(name, ws_path)
    ws_dir = ws_path.parent

    deploy_yml = project_deploy_yml(project, ws_dir)
    if not deploy_yml.exists():
        _err(f"deploy.yml not found. Run: dlane scaffold {name}")

    import yaml as _yaml
    try:
        raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    targets_raw = raw.get("targets") or {}
    t_raw = targets_raw.get(target) or {}
    if not t_raw:
        _err(f"Target '{target}' not found in deploy.yml.")

    host = str(t_raw.get("host") or "").strip()
    user = str(t_raw.get("user") or "deploy").strip()
    remote_dir = str(t_raw.get("deploy_dir") or "").strip()

    if not host or not remote_dir:
        _err(f"deploy.yml target '{target}' is missing host or deploy_dir.\n  Edit: {deploy_yml}")

    dest = f"{user}@{host}"
    base = ws_dir / project.path / ".deploylane"

    typer.secho(f"[{name}] Install push {'(dry run)' if not yes else ''} → {dest}:{remote_dir}", fg=typer.colors.CYAN)

    # install.sh
    install_src = base / "install.sh"
    if not install_src.exists():
        _err(f"install.sh not found. Run: dlane scaffold {name}")

    try:
        copy_file(install_src, dest, f"{remote_dir}/.deploylane/install.sh", dry_run=(not yes))
        typer.echo(f"  install.sh → {remote_dir}/.deploylane/install.sh")
    except (RemoteError, Exception) as e:
        _err(f"Could not push install.sh: {e}")

    # nginx snippets (bluegreen only)
    nginx_dir = base / "nginx"
    if nginx_dir.exists():
        for f_ in nginx_dir.iterdir():
            if f_.is_file():
                try:
                    copy_file(f_, dest, f"{remote_dir}/.deploylane/nginx/{f_.name}", dry_run=(not yes))
                    typer.echo(f"  {f_.name} → {remote_dir}/.deploylane/nginx/{f_.name}")
                except Exception:
                    typer.secho(f"  Warning: could not push {f_.name}", fg=typer.colors.YELLOW)

    # sudoers
    sudoers_src = base / "sudoers" / "nginx-bg-switch"
    if sudoers_src.exists():
        try:
            copy_file(sudoers_src, dest, f"{remote_dir}/.deploylane/sudoers/nginx-bg-switch", dry_run=(not yes))
            typer.echo(f"  nginx-bg-switch → {remote_dir}/.deploylane/sudoers/nginx-bg-switch")
        except Exception:
            typer.secho("  Warning: could not push sudoers file.", fg=typer.colors.YELLOW)

    typer.echo("")
    install_cmd = f"sudo bash {remote_dir}/.deploylane/install.sh"
    typer.secho(f"[{name}] Running: {install_cmd}", fg=typer.colors.CYAN)
    try:
        ssh_run_interactive(dest, install_cmd, dry_run=(not yes))
    except (RemoteError, Exception) as e:
        _err(f"install.sh failed: {e}")

    typer.secho(f"[{name}] Install OK", fg=typer.colors.GREEN)


@deploy_app.command("history")
def deploy_history(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias (omit for all projects)"),
    limit: int = typer.Option(20, "--limit", help="Number of entries to show"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Show recent deployment history from the workspace log."""
    ws_path = _resolve_ws(file)
    entries = read_log(ws_path, project=name, limit=limit)

    if not entries:
        typer.echo("No deployment history found.")
        return

    SEP = "  "
    w_proj   = max(max(len(e.get("project", "")) for e in entries), 7)
    w_target = max(max(len(e.get("target", ""))  for e in entries), 6)
    w_image  = max(max(len(e.get("image_tag", "")) for e in entries), 8)
    w_strat  = max(max(len(e.get("strategy", "")) for e in entries), 5)

    typer.echo("")
    typer.secho(
        SEP + f"{'TIMESTAMP':<19}" + SEP
        + f"{'PROJECT':<{w_proj}}" + SEP
        + f"{'TARGET':<{w_target}}" + SEP
        + f"{'IMAGE TAG':<{w_image}}" + SEP
        + f"{'STRATEGY':<{w_strat}}" + SEP
        + "STATUS",
        bold=True,
    )
    typer.echo(
        SEP + "─" * 19 + SEP
        + "─" * w_proj + SEP
        + "─" * w_target + SEP
        + "─" * w_image + SEP
        + "─" * w_strat + SEP
        + "─" * 7
    )

    for e in entries:
        status = e.get("status", "?")
        if status == "ok":
            status_styled = typer.style("ok     ", fg=typer.colors.GREEN)
        elif status == "dry-run":
            status_styled = typer.style("dry-run", fg=typer.colors.CYAN)
        else:
            status_styled = typer.style("failed ", fg=typer.colors.RED)

        typer.echo(
            SEP + f"{e.get('timestamp','')[:19]:<19}" + SEP
            + f"{e.get('project',''):<{w_proj}}" + SEP
            + f"{e.get('target',''):<{w_target}}" + SEP
            + f"{e.get('image_tag',''):<{w_image}}" + SEP
            + f"{e.get('strategy',''):<{w_strat}}" + SEP
            + status_styled
        )

    typer.echo("")
