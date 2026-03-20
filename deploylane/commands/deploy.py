from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import typer

from ..deployspec import load_deployspec
from ..deploylog import read_log, append_log, make_entry
from ..remote import copy_file, RemoteError, ssh_run_interactive, read_remote_file, remote_file_exists, ssh_capture
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


def _fetch_remote_compose(dest: str, remote_dir: str) -> Optional[str]:
    """Fetch remote docker-compose.yml. Returns None if not present."""
    try:
        return read_remote_file(dest, f"{remote_dir}/docker-compose.yml")
    except RemoteError:
        return None


def _compose_diff(remote_content: str, local_content: str) -> List[str]:
    import difflib
    return list(difflib.unified_diff(
        remote_content.splitlines(keepends=True),
        local_content.splitlines(keepends=True),
        fromfile="server:docker-compose.yml",
        tofile="local:docker-compose.yml",
    ))


def _push_project(
    ws_path: Path,
    name: str,
    target: str,
    yes: bool,
    force: bool = False,
) -> Tuple[bool, str, Dict]:
    """Push .env + compose + deploy.sh to server. Returns (ok, error, changes)."""
    ws_dir = ws_path.parent
    changes: Dict[str, str] = {}

    try:
        ws = load_workspace(ws_path)
        project = next((p for p in ws.projects if p.name == name), None)
        if not project:
            return False, f"Project '{name}' not found in workspace.", changes

        deploy_yml = project_deploy_yml(project, ws_dir)
        if not deploy_yml.exists():
            return False, f"deploy.yml not found: {deploy_yml}\nRun: dlane scaffold {name}", changes

        try:
            spec = load_deployspec(deploy_yml)
        except Exception as e:
            return False, f"Invalid deploy.yml: {e}", changes

        if target not in spec.targets:
            return False, f"Unknown target '{target}'. Available: {', '.join(sorted(spec.targets.keys()))}", changes

        t = spec.targets[target]

        if not t.host:
            return False, (
                f"deploy.yml target '{target}': 'host' is empty.\n"
                f"  Edit: {deploy_yml}\n"
                f"  Set the server IP or hostname under targets.{target}.host"
            ), changes
        if not t.user:
            return False, (
                f"deploy.yml target '{target}': 'user' is empty.\n"
                f"  Edit: {deploy_yml}"
            ), changes
        if not t.deploy_dir:
            return False, (
                f"deploy.yml target '{target}': 'deploy_dir' is empty.\n"
                f"  Edit: {deploy_yml}"
            ), changes

        strategy = (getattr(t, "strategy", "plain") or "plain").strip()
        dest = f"{t.user}@{t.host}"
        remote_dir = t.deploy_dir
        base = ws_dir / project.path / ".deploylane"

        typer.echo(f"  {'(dry run) ' if not yes else ''}{dest}:{remote_dir}")

        # .env
        env_src = base / "env" / f"{target}.env"
        local_env = base / "env" / f"{target}.local.env"
        if not env_src.exists():
            return False, f".env template not found: {env_src}\nRun: dlane scaffold {name}", changes
        if remote_file_exists(dest, f"{remote_dir}/.env"):
            typer.secho(f"  .env already exists on server — skipped (pull to inspect)", fg=typer.colors.YELLOW)
            changes["env"] = "skipped"
        else:
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
                changes["env"] = "pushed"
            except (RemoteError, Exception) as e:
                return False, f"Push .env failed: {e}", changes
            finally:
                if tmp_env and tmp_env.exists():
                    tmp_env.unlink()

        # deploy.sh
        script_src = base / "scripts" / "deploy.sh"
        if script_src.exists():
            if not force:
                try:
                    remote_sh = read_remote_file(dest, f"{remote_dir}/deploy.sh")
                    local_sh = script_src.read_text(encoding="utf-8")
                    if remote_sh.strip() != local_sh.strip():
                        import difflib
                        diff = list(difflib.unified_diff(
                            remote_sh.splitlines(keepends=True),
                            local_sh.splitlines(keepends=True),
                            fromfile="server:deploy.sh",
                            tofile="local:deploy.sh",
                        ))
                        typer.secho("  ⚠ Server deploy.sh differs from local:", fg=typer.colors.YELLOW)
                        for line in diff:
                            if line.startswith("+"):
                                typer.secho(line, fg=typer.colors.GREEN, nl=False)
                            elif line.startswith("-"):
                                typer.secho(line, fg=typer.colors.RED, nl=False)
                            else:
                                typer.echo(line, nl=False)
                        return False, "Server deploy.sh has changes. Run 'dlane deploy pull' first, or use --force to override.", changes
                    changes["deploy_sh"] = "unchanged"
                except RemoteError:
                    pass  # not on server yet, first push
            try:
                copy_file(script_src, dest, f"{remote_dir}/deploy.sh", dry_run=(not yes))
                typer.echo(f"  deploy.sh → {remote_dir}/deploy.sh")
                changes["deploy_sh"] = changes.get("deploy_sh") or "pushed"
            except Exception as e:
                typer.secho(f"  Warning: could not push deploy.sh: {e}", fg=typer.colors.YELLOW)
                changes["deploy_sh"] = "failed"

        # docker-compose.yml
        compose_src = base / "compose" / f"{strategy}.yml"
        if not compose_src.exists():
            compose_src = base / "compose" / "plain.yml"
        if compose_src.exists():
            if not force:
                remote_compose = _fetch_remote_compose(dest, remote_dir)
                if remote_compose is not None:
                    local_compose = compose_src.read_text(encoding="utf-8")
                    if remote_compose.strip() != local_compose.strip():
                        diff = _compose_diff(remote_compose, local_compose)
                        typer.secho("  ⚠ Server compose differs from local:", fg=typer.colors.YELLOW)
                        for line in diff:
                            if line.startswith("+"):
                                typer.secho(line, fg=typer.colors.GREEN, nl=False)
                            elif line.startswith("-"):
                                typer.secho(line, fg=typer.colors.RED, nl=False)
                            else:
                                typer.echo(line, nl=False)
                        return False, "Server has uncommitted changes. Run 'dlane deploy pull' first, or use --force to override.", changes
                    changes["compose"] = "unchanged"
                else:
                    changes["compose"] = "pushed"
            try:
                copy_file(compose_src, dest, f"{remote_dir}/docker-compose.yml", dry_run=(not yes))
                typer.echo(f"  .deploylane/compose/{strategy}.yml → {remote_dir}/docker-compose.yml")
                changes["compose"] = changes.get("compose") or "pushed"
            except Exception as e:
                typer.secho(f"  Warning: could not push docker-compose.yml: {e}", fg=typer.colors.YELLOW)
                changes["compose"] = "failed"

    except Exception as e:
        return False, str(e), changes

    return True, "", changes


# ─── Commands ─────────────────────────────────────────────────────────────────



def _pull_file(dest: str, remote_path: str, local_path: Path, label: str) -> None:
    """Pull a single file from server. Shows diff and asks confirmation if different."""
    import difflib
    try:
        remote_content = read_remote_file(dest, remote_path)
    except RemoteError:
        typer.secho(f"    {label}: not found on server — skipped", fg=typer.colors.YELLOW)
        return

    if not local_path.exists():
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(remote_content, encoding="utf-8")
        typer.secho(f"    {label}: created", fg=typer.colors.GREEN)
        return

    local_content = local_path.read_text(encoding="utf-8")
    if remote_content.strip() == local_content.strip():
        typer.secho(f"    {label}: up to date", fg=typer.colors.GREEN)
        return

    diff = list(difflib.unified_diff(
        remote_content.splitlines(keepends=True),
        local_content.splitlines(keepends=True),
        fromfile=f"server:{label}",
        tofile=f"local:{label}",
    ))
    typer.secho(f"    {label}: diff (server → local):", fg=typer.colors.YELLOW)
    for line in diff:
        if line.startswith("+"):
            typer.secho(line, fg=typer.colors.GREEN, nl=False)
        elif line.startswith("-"):
            typer.secho(line, fg=typer.colors.RED, nl=False)
        else:
            typer.echo(line, nl=False)
    typer.echo("")
    typer.confirm(f"    Overwrite local {label}?", abort=True)
    local_path.write_text(remote_content, encoding="utf-8")
    typer.secho(f"    {label}: updated", fg=typer.colors.GREEN)


def _pull_target(
    name: str,
    t_name: str,
    t_raw: dict,
    strategy_top: str,
    base: Path,
    app_name: str,
) -> None:
    """Pull compose, .env and nginx configs from a single target's server."""
    host = str(t_raw.get("host") or "").strip()
    user = str(t_raw.get("user") or "deploy").strip()
    remote_dir = str(t_raw.get("deploy_dir") or "").strip()
    strategy = str(t_raw.get("strategy") or strategy_top).strip()

    if not host or not remote_dir:
        typer.secho(f"  [{t_name}] Skipped — missing host or deploy_dir", fg=typer.colors.YELLOW)
        return

    dest = f"{user}@{host}"
    typer.secho(f"  [{t_name}] Pulling from {dest}:{remote_dir}", fg=typer.colors.CYAN)

    # compose
    compose_local = base / "compose" / f"{strategy}.yml"
    if not compose_local.exists():
        compose_local = base / "compose" / "plain.yml"
    _pull_file(dest, f"{remote_dir}/docker-compose.yml", compose_local, "docker-compose.yml")

    # deploy.sh
    _pull_file(dest, f"{remote_dir}/deploy.sh", base / "scripts" / "deploy.sh", "deploy.sh")

    # .env
    env_local = base / "env" / f"{t_name}.env"
    _pull_file(dest, f"{remote_dir}/.env", env_local, f"{t_name}.env")

    # nginx + sudoers (bluegreen only) — files live in system dirs after install
    if strategy == "bluegreen":
        nginx_pulls = [
            (f"{app_name}-upstream-blue.conf",  "/etc/nginx/snippets"),
            (f"{app_name}-upstream-green.conf", "/etc/nginx/snippets"),
            (f"00-{app_name}-upstream.conf",    "/etc/nginx/sites-available"),
        ]
        for fname, nginx_dir in nginx_pulls:
            _pull_file(
                dest,
                f"{nginx_dir}/{fname}",
                base / "nginx" / fname,
                f"nginx/{fname}",
            )

        # sudoers requires root to read — cannot pull via SSH as deploy user


@deploy_app.command("pull")
def deploy_pull(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    target: Optional[str] = typer.Option(None, "--target", help="Pull specific target only"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Fetch docker-compose.yml from all target servers and update local copies."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    project = _load_project(name, ws_path)
    ws_dir = ws_path.parent

    deploy_yml = project_deploy_yml(project, ws_dir)
    if not deploy_yml.exists():
        _err(f"deploy.yml not found. Run: dlane scaffold {name}")

    import yaml as _yaml
    raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    targets_raw = raw.get("targets") or {}
    strategy_top = str(raw.get("strategy") or "plain").strip()
    app_name = name.replace("-", "_")
    base = ws_dir / project.path / ".deploylane"

    typer.secho(f"[{name}] Pull", fg=typer.colors.CYAN, bold=True)

    if target:
        t_raw = targets_raw.get(target)
        if not t_raw:
            _err(f"Target '{target}' not found in deploy.yml.")
        _pull_target(name, target, t_raw, strategy_top, base, app_name)
    else:
        for t_name, t_raw in targets_raw.items():
            if isinstance(t_raw, dict):
                _pull_target(name, t_name, t_raw, strategy_top, base, app_name)

    typer.secho(f"[{name}] Pull done.", fg=typer.colors.GREEN)


@deploy_app.command("push")
def deploy_push(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias (omit with --all or --tag)"),
    target: Optional[str] = typer.Option(None, "--target", help="Deploy target name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Actually push (default: dry run)"),
    force: bool = typer.Option(False, "--force", help="Skip server compose check and push anyway"),
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

    def _log_push(project_name: str, resolved_target: str, ok: bool, err: str, changes: Dict) -> None:
        if yes:  # only log actual pushes, not dry-runs
            ws = load_workspace(ws_path)
            proj = next((p for p in ws.projects if p.name == project_name), None)
            entry = make_entry(
                project=project_name,
                gitlab_project=proj.gitlab_project if proj else "",
                target=resolved_target,
                image_tag="",
                strategy="",
                host="",
                deploy_dir="",
                status="ok" if ok else "failed",
                error=err,
            )
            entry["changes"] = changes
            append_log(ws_path, entry)

    if len(names) == 1:
        resolved_target = _resolve_target(names[0], target, ws_path)
        typer.secho(f"[{names[0]}] Push {'(dry run)' if not yes else ''} → target={resolved_target}", fg=typer.colors.CYAN)
        ok, err, changes = _push_project(ws_path, names[0], resolved_target, yes, force=force)
        _log_push(names[0], resolved_target, ok, err, changes)
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
        ok, err, changes = _push_project(ws_path, project_name, resolved_target, yes, force=force)
        _log_push(project_name, resolved_target, ok, err, changes)
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


def _parse_remote_env(dest: str, remote_dir: str) -> Dict[str, str]:
    """Read and parse .env from server into a dict."""
    try:
        content = read_remote_file(dest, f"{remote_dir}/.env")
    except RemoteError:
        return {}
    result: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip()
    return result


def _compose_service_running(dest: str, remote_dir: str, service_name: str) -> bool:
    """Check if a docker compose service is running."""
    try:
        out = ssh_capture(dest, f"cd {remote_dir} && docker compose ps --status running --services 2>/dev/null")
        return service_name in out.splitlines()
    except RemoteError:
        return False


@deploy_app.command("status")
def deploy_status(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Show running container status for all targets of a project."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    project = _load_project(name, ws_path)
    ws_dir = ws_path.parent

    deploy_yml = project_deploy_yml(project, ws_dir)
    if not deploy_yml.exists():
        _err(f"deploy.yml not found. Run: dlane scaffold {name}")

    import yaml as _yaml
    raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    targets_raw = raw.get("targets") or {}
    strategy_top = str(raw.get("strategy") or "plain").strip()
    app_name = name.replace("-", "_")
    tag_key = app_name.upper() + "_TAG"

    typer.secho(f"[{name}] Status", fg=typer.colors.CYAN, bold=True)
    typer.echo("")

    for t_name, t_raw in targets_raw.items():
        if not isinstance(t_raw, dict):
            continue

        host = str(t_raw.get("host") or "").strip()
        user = str(t_raw.get("user") or "deploy").strip()
        remote_dir = str(t_raw.get("deploy_dir") or "").strip()
        strategy = str(t_raw.get("strategy") or strategy_top).strip()

        if not host or not remote_dir:
            typer.secho(f"  [{t_name}] Skipped — missing host or deploy_dir", fg=typer.colors.YELLOW)
            continue

        dest = f"{user}@{host}"
        typer.secho(f"  [{t_name}] {host}", bold=True)

        env = _parse_remote_env(dest, remote_dir)

        if strategy == "bluegreen":
            active_color = env.get("ACTIVE_COLOR", "?")
            tag_blue  = env.get(f"{tag_key}_BLUE", "—")
            tag_green = env.get(f"{tag_key}_GREEN", "—")

            for color, tag in [("blue", tag_blue), ("green", tag_green)]:
                service = f"{app_name}_{color}"
                running = _compose_service_running(dest, remote_dir, service)
                is_active = color == active_color
                status_icon = typer.style("✓ running", fg=typer.colors.GREEN) if running else typer.style("✗ stopped", fg=typer.colors.RED)
                active_label = typer.style(" ← active", fg=typer.colors.CYAN) if is_active else ""
                typer.echo(f"    {color:<6} {status_icon}  {tag}{active_label}")
        else:
            tag = env.get(tag_key, "—")
            running = _compose_service_running(dest, remote_dir, app_name)
            status_icon = typer.style("✓ running", fg=typer.colors.GREEN) if running else typer.style("✗ stopped", fg=typer.colors.RED)
            typer.echo(f"    {status_icon}  {tag}")

        typer.echo("")


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
        changes = e.get("changes") or {}
        if changes:
            parts = [f"{k}:{v}" for k, v in changes.items()]
            typer.secho(f"    └ {', '.join(parts)}", fg=typer.colors.BRIGHT_BLACK)

    typer.echo("")
