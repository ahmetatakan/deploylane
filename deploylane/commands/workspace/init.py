from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import typer

from ...stacks import VALID_STRATEGIES, is_valid_strategy
from .._utils import _err


def _read_vars_for_target(vars_yml_path: Path, target: str) -> dict:
    """Read host/user/deploy_dir from vars.yml using {TARGET_UPPER}_{FIELD} pattern."""
    if not vars_yml_path.exists():
        return {}
    try:
        import yaml as _yaml
        data = _yaml.safe_load(vars_yml_path.read_text(encoding="utf-8")) or {}
        variables = data.get("variables") or {}

        def _val(key: str) -> str:
            entry = variables.get(key)
            if isinstance(entry, dict):
                return str(entry.get("value") or "").strip()
            return ""

        prefix = target.upper()
        return {
            "host":       _val(f"{prefix}_HOST"),
            "user":       _val(f"{prefix}_USER"),
            "deploy_dir": _val(f"{prefix}_DEPLOY_DIR"),
        }
    except Exception:
        return {}


def _do_init(
    strategy: str,
    project: str,
    app_name: Optional[str],
    target: str,
    host: str,
    user: str,
    deploy_dir: str,
    registry_host: str,
    force: bool,
    ws_name: Optional[str] = None,
    skip_vars: bool = False,
) -> None:
    resolved_strategy = strategy or "plain"
    # workspace alias (underscored) takes priority — must match nginx/compose/deploy.sh APP_NAME
    resolved_app_name = app_name or (ws_name.replace("-", "_") if ws_name else None) or project.split("/")[-1]
    port_blue = 8080
    port_green = 8081

    base = Path(".deploylane")
    deploy_yml_path = base / "deploy.yml"
    vars_yml_path = base / "vars.yml"
    script_dst = base / "scripts" / "deploy.sh"
    script_src = Path(__file__).parent.parent.parent / "scripts" / "deploy.sh"

    if deploy_yml_path.exists() and not force:
        _err(f"{deploy_yml_path} already exists. Use --force to overwrite.")

    base.mkdir(exist_ok=True)
    (base / "scripts").mkdir(exist_ok=True)

    # Pre-fill host/user/deploy_dir from vars.yml if available (vars get already run)
    pre = _read_vars_for_target(vars_yml_path, target)
    if not host and pre.get("host"):
        host = pre["host"]
        typer.secho(f"  Using {target.upper()}_HOST from vars.yml: {host}", fg=typer.colors.CYAN)
    if not user and pre.get("user"):
        user = pre["user"]
    if not deploy_dir and pre.get("deploy_dir"):
        deploy_dir = pre["deploy_dir"]
        typer.secho(f"  Using {target.upper()}_DEPLOY_DIR from vars.yml: {deploy_dir}", fg=typer.colors.CYAN)

    # Try to pre-fill staging from vars.yml too
    pre_staging = _read_vars_for_target(vars_yml_path, "staging")
    staging_host       = pre_staging.get("host", "")
    staging_user       = pre_staging.get("user") or user
    staging_deploy_dir = pre_staging.get("deploy_dir") or f"/home/{staging_user}/{resolved_app_name}-staging"

    resolved_deploy_dir = deploy_dir or f"/home/{user}/{resolved_app_name}"

    deploy_yml_content = f"""\
version: 1
strategy: {resolved_strategy}
project: {project}
default_target: {target}

app:
  name: {resolved_app_name}
  registry_host: "{registry_host}"

targets:
  {target}:
    host: "{host}"{"" if host else "          # fill in: your server IP"}
    user: "{user}"
    deploy_dir: "{resolved_deploy_dir}"
    env_scope: production
    strategy: {resolved_strategy}
    health_host: ""         # optional: domain for nginx health check
    ports:
      blue: {port_blue}
      green: {port_green}

  staging:
    host: "{staging_host}"{"" if staging_host else "                # fill in: staging server IP (can be same as prod)"}
    user: "{staging_user}"
    deploy_dir: "{staging_deploy_dir}"
    env_scope: staging
    strategy: plain
    health_host: ""
    ports:
      blue: {port_blue + 100}
      green: {port_green + 100}
"""
    deploy_yml_path.write_text(deploy_yml_content, encoding="utf-8")
    typer.secho(f"  Created {deploy_yml_path}", fg=typer.colors.GREEN)

    vars_yml_content = f"""\
project: {project}
scope: "*"
variables:
  REGISTRY_USER:
    value: ""
    masked: true
    protected: true
    environment_scope: "*"
  REGISTRY_PASS:
    value: ""
    masked: true
    protected: true
    environment_scope: "*"
  REGISTRY_HOST:
    value: "{registry_host}"
    masked: false
    protected: false
    environment_scope: "*"
"""

    if skip_vars:
        typer.secho(f"  Skipped {vars_yml_path} (preserved)", fg=typer.colors.YELLOW)
    elif not vars_yml_path.exists() or force:
        vars_yml_path.write_text(vars_yml_content, encoding="utf-8")
        typer.secho(f"  Created {vars_yml_path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  Skipped {vars_yml_path} (already exists)", fg=typer.colors.YELLOW)

    if script_src.exists():
        shutil.copy2(str(script_src), str(script_dst))
        script_dst.chmod(0o755)
        typer.secho(f"  Created {script_dst}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  Warning: deploy.sh template not found at {script_src}", fg=typer.colors.YELLOW)

    alias = ws_name or project.split("/")[-1]

    typer.echo("")
    typer.secho("Done!", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Project  : {project}")
    typer.echo(f"  Strategy : {resolved_strategy}")
    typer.echo(f"  Files    : {deploy_yml_path.parent}/")
    typer.echo("")
    typer.secho("Next steps:", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  1) Edit {deploy_yml_path}")
    typer.echo(f"       → Set host, user, deploy_dir for each target")
    typer.echo(f"  2) Fill in {vars_yml_path}")
    typer.echo(f"       → Set values for PROD_HOST, PROD_SSH_KEY, REGISTRY_USER, REGISTRY_PASS, etc.")
    typer.echo(f"  3) dlane vars apply {alias}")
    typer.echo(f"       → Push variables to GitLab CI")
    typer.echo(f"  4) dlane deploy push {alias} --yes")
    typer.echo(f"       → Sync .env + docker-compose.yml + deploy.sh to the server")
    typer.echo(f"  5) dlane deploy install {alias} --yes")
    typer.echo(f"       → One-time server setup: nginx + sudoers + install.sh")
    typer.echo("")
    typer.echo(f"  Tip: copy {deploy_yml_path.parent}/ci/.gitlab-ci.yml to your project repo")


def _run_init_in_dir(
    project_path: Path,
    strategy: str,
    project: str,
    app_name: Optional[str] = None,
    target: str = "prod",
    host: str = "",
    user: str = "deploy",
    deploy_dir: str = "",
    registry_host: str = "",
    force: bool = False,
    ws_name: Optional[str] = None,
    skip_vars: bool = False,
) -> None:
    orig = Path.cwd()
    try:
        os.chdir(project_path)
        _do_init(
            strategy=strategy, project=project, app_name=app_name, target=target,
            host=host, user=user, deploy_dir=deploy_dir, registry_host=registry_host,
            force=force, ws_name=ws_name, skip_vars=skip_vars,
        )
    finally:
        os.chdir(orig)


def workspace_scaffold(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing compose files"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """
    Regenerate .deploylane/ files from the existing deploy.yml.

    - deploy.yml is created only if it doesn't exist (never overwritten).
    - compose/ files are created only if they don't exist (use --force to overwrite).
    - Always regenerates: deploy.sh, env/, nginx/, sudoers/, install.sh.
    """
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    from ._utils import _resolve_ws
    from ...workspace import load_workspace, project_deploy_yml

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")

    project_path = (ws_path.parent / project.path).resolve()
    script_dst = project_path / ".deploylane" / "scripts" / "deploy.sh"
    script_src = Path(__file__).parent.parent.parent / "scripts" / "deploy.sh"

    (project_path / ".deploylane" / "scripts").mkdir(parents=True, exist_ok=True)

    if not script_src.exists():
        _err(f"deploy.sh not found in package: {script_src}")

    shutil.copy2(str(script_src), str(script_dst))
    script_dst.chmod(0o755)
    typer.secho(f"[{name}] Updated deploy.sh", fg=typer.colors.GREEN)

    deploy_yml = project_deploy_yml(project, ws_path.parent)

    if not deploy_yml.exists():
        _run_init_in_dir(
            project_path,
            strategy=project.strategy,
            project=project.gitlab_project,
            ws_name=name,
            skip_vars=True,  # scaffold never touches vars.yml — managed by 'vars get/apply'
        )
        typer.secho(f"[{name}] Created deploy.yml (fill in host/user/deploy_dir)", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"[{name}] deploy.yml exists — reading targets from it", fg=typer.colors.CYAN)
        _sync_deploy_yml_from_vars(name, project_path, ws_path)

    # Always regenerate derived files from current deploy.yml
    _scaffold_infra(name, project_path, ws_path, force=force)

    typer.secho(f"[{name}] Scaffold OK", fg=typer.colors.GREEN)


def _sync_deploy_yml_from_vars(name: str, project_path: Path, ws_path: Path) -> None:
    """Update host/user/deploy_dir in deploy.yml from vars.yml (non-empty vars win).

    Rule: vars.yml value is non-empty → overwrite deploy.yml field.
          vars.yml value is empty or missing → keep existing deploy.yml value.
    """
    import yaml as _yaml
    from ...workspace import load_workspace, project_deploy_yml

    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        return

    deploy_yml = project_deploy_yml(project, ws_path.parent)
    vars_yml = project_path / ".deploylane" / "vars.yml"

    if not deploy_yml.exists() or not vars_yml.exists():
        return

    try:
        raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
        vars_data = _yaml.safe_load(vars_yml.read_text(encoding="utf-8")) or {}
    except Exception:
        return

    variables = vars_data.get("variables") or {}

    def _var(key: str) -> str:
        entry = variables.get(key)
        if isinstance(entry, dict):
            return str(entry.get("value") or "").strip()
        return ""

    targets_raw = raw.get("targets") or {}
    changed: list[str] = []

    for t_name in list(targets_raw.keys()):
        t_data = targets_raw[t_name]
        if not isinstance(t_data, dict):
            continue
        prefix = t_name.upper()

        for field, var_key in [("host", f"{prefix}_HOST"), ("user", f"{prefix}_USER"), ("deploy_dir", f"{prefix}_DEPLOY_DIR")]:
            val = _var(var_key)
            if val and t_data.get(field, "") != val:
                t_data[field] = val
                changed.append(f"{t_name}.{field} ← {var_key}")


    if changed:
        deploy_yml.write_text(_yaml.dump(raw, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        for c in changed:
            typer.secho(f"  synced {c}", fg=typer.colors.CYAN)


def _render_env_template(strategy: str, ctx: dict) -> str:
    app_name = ctx["app_name"]
    registry_host = ctx["registry_host"]
    port_blue = ctx["port_blue"]
    port_green = ctx["port_green"]
    plain_port = ctx["plain_port"]
    tag_key = ctx["tag_key"]

    lines = [
        f"APP_NAME={app_name}",
        f"DEPLOY_STRATEGY={strategy}",
        f"REGISTRY_HOST={registry_host}",
        "",
    ]

    if strategy == "bluegreen":
        lines += [
            f"APP_PORT_BLUE={port_blue}",
            f"APP_PORT_GREEN={port_green}",
            "",
            "# --- state (managed by deploy.sh, do not edit manually) ---",
            "ACTIVE_COLOR=blue",
            f"{tag_key}_BLUE=",
            f"{tag_key}_GREEN=",
        ]
    else:
        lines += [
            f"APP_PORT={plain_port}",
            "",
            "# --- state (managed by deploy.sh, do not edit manually) ---",
            f"{tag_key}=",
        ]

    lines.append("")
    return "\n".join(lines)


def _scaffold_infra(name: str, project_path: Path, ws_path: Path, force: bool = False) -> None:
    """Generate nginx snippets, compose template, sudoers and install.sh from deploy.yml."""
    import yaml as _yaml
    from ...templates import (
        render_nginx_blue, render_nginx_green, render_nginx_site_include,
        render_sudoers, render_install_sh, render_compose, render_gitlab_ci,
    )
    from ...workspace import load_workspace, project_deploy_yml

    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        return

    deploy_yml = project_deploy_yml(project, ws_path.parent)
    if not deploy_yml.exists():
        return

    try:
        raw = _yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    except Exception:
        return

    strategy_top = str(raw.get("strategy") or raw.get("stack") or "plain").strip().lower()
    app_raw = raw.get("app") or {}
    # Use workspace alias as app_name (matches APP_NAME in .env and deploy.sh logic)
    app_name = name.replace("-", "_")
    registry_host = str(app_raw.get("registry_host") or "").strip()
    default_target = str(raw.get("default_target") or "prod").strip()
    targets_raw = raw.get("targets") or {}
    t_raw = targets_raw.get(default_target) or next(iter(targets_raw.values()), {}) if targets_raw else {}

    strategy = str(t_raw.get("strategy") or strategy_top).strip()
    deploy_user = str(t_raw.get("user") or "deploy").strip()
    deploy_dir = str(t_raw.get("deploy_dir") or f"/home/{deploy_user}/{app_name}").strip()
    port_blue = str(t_raw.get("ports", {}).get("blue") if isinstance(t_raw.get("ports"), dict) else 8080)
    port_green = str(t_raw.get("ports", {}).get("green") if isinstance(t_raw.get("ports"), dict) else 8081)
    plain_port = str(t_raw.get("ports", {}).get("plain") if isinstance(t_raw.get("ports"), dict) else 8080)

    tag_key = app_name.upper().replace("-", "_") + "_TAG"
    ctx = {
        "app_name":      app_name,
        "registry_host": registry_host,
        "gitlab_project": project.gitlab_project,
        "tag_key":       tag_key,
        "port_blue":     port_blue,
        "port_green":    port_green,
        "plain_port":    plain_port,
        "deploy_user":   deploy_user,
        "deploy_dir":    deploy_dir,
        "strategy":      strategy,
    }

    base = project_path / ".deploylane"

    # ── compose files per strategy (.deploylane/compose/<strategy>.yml) ───────
    # Collect all unique strategies across all targets
    unique_strategies: set = set()
    for t_name, t_data in targets_raw.items():
        if isinstance(t_data, dict):
            t_strategy = str(t_data.get("strategy") or strategy_top).strip() or "plain"
            unique_strategies.add(t_strategy)
    if not unique_strategies:
        unique_strategies.add(strategy)

    compose_dir = base / "compose"
    compose_dir.mkdir(parents=True, exist_ok=True)
    created_compose, skipped_compose = [], []
    for s in sorted(unique_strategies):
        compose_dst = compose_dir / f"{s}.yml"
        if not compose_dst.exists() or force:
            compose_dst.write_text(render_compose(s, ctx), encoding="utf-8")
            created_compose.append(f"{s}.yml")
        else:
            skipped_compose.append(f"{s}.yml")
    if created_compose:
        typer.secho(
            f"  Created .deploylane/compose/ ({', '.join(created_compose)})",
            fg=typer.colors.GREEN,
        )
    if skipped_compose:
        typer.secho(
            f"  Skipped .deploylane/compose/ ({', '.join(skipped_compose)}) — already exists",
            fg=typer.colors.YELLOW,
        )

    # ── .env base template per target (.deploylane/env/<target>.env) ──────────
    env_dir = base / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    for t_name, t_data in targets_raw.items():
        if not isinstance(t_data, dict):
            continue
        t_strategy = str(t_data.get("strategy") or strategy_top).strip() or "plain"
        ports = t_data.get("ports") or {}
        t_ctx = dict(ctx)
        t_ctx["port_blue"]  = str(ports.get("blue",  8080) if isinstance(ports, dict) else 8080)
        t_ctx["port_green"] = str(ports.get("green", 8081) if isinstance(ports, dict) else 8081)
        t_ctx["plain_port"] = str(ports.get("plain", ports.get("blue", 8080)) if isinstance(ports, dict) else 8080)
        env_file = env_dir / f"{t_name}.env"
        env_file.write_text(_render_env_template(t_strategy, t_ctx), encoding="utf-8")

        local_env_file = env_dir / f"{t_name}.local.env"
        if not local_env_file.exists():
            local_env_file.write_text(
                f"# Project-specific overrides for target: {t_name}\n"
                f"# Keys here are merged on top of {t_name}.env during 'deploy push'.\n"
                "# This file is gitignored — safe to store secrets here.\n",
                encoding="utf-8",
            )
    # ── .gitignore — keep *.local.env out of version control ─────────────────
    gitignore = env_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*.local.env\n", encoding="utf-8")

    typer.secho(
        f"  Created .deploylane/env/ ({', '.join(t + '.env' for t in sorted(targets_raw))})",
        fg=typer.colors.GREEN,
    )

    # ── nginx + sudoers (bluegreen only, based on default target strategy) ────
    if strategy == "bluegreen":
        nginx_dir = base / "nginx"
        nginx_dir.mkdir(parents=True, exist_ok=True)

        (nginx_dir / f"{app_name}-upstream-blue.conf").write_text(render_nginx_blue(ctx), encoding="utf-8")
        (nginx_dir / f"{app_name}-upstream-green.conf").write_text(render_nginx_green(ctx), encoding="utf-8")
        (nginx_dir / f"00-{app_name}-upstream.conf").write_text(render_nginx_site_include(ctx), encoding="utf-8")
        typer.secho(f"  Created .deploylane/nginx/ (3 files)", fg=typer.colors.GREEN)

        sudoers_dir = base / "sudoers"
        sudoers_dir.mkdir(parents=True, exist_ok=True)
        (sudoers_dir / "nginx-bg-switch").write_text(render_sudoers(ctx), encoding="utf-8")
        typer.secho(f"  Created .deploylane/sudoers/nginx-bg-switch", fg=typer.colors.GREEN)

    # ── install.sh ────────────────────────────────────────────────────────────
    install_dst = base / "install.sh"
    install_dst.write_text(render_install_sh(ctx), encoding="utf-8")
    install_dst.chmod(0o755)
    typer.secho(f"  Created .deploylane/install.sh", fg=typer.colors.GREEN)

    # ── vars.yml — standard placeholders (create once, never overwrite) ────────
    vars_yml = base / "vars.yml"
    if not vars_yml.exists():
        import yaml as _yaml
        variables: dict = {}
        # Per-target operational vars
        for t_name in sorted(targets_raw.keys()):
            prefix = t_name.upper()
            variables[f"{prefix}_HOST"]       = {"value": "", "masked": False,  "protected": False, "environment_scope": "*"}
            variables[f"{prefix}_USER"]       = {"value": "", "masked": False,  "protected": False, "environment_scope": "*"}
            variables[f"{prefix}_DEPLOY_DIR"] = {"value": "", "masked": True,   "protected": False, "environment_scope": "*"}
            variables[f"{prefix}_SSH_KEY"]    = {"value": "", "masked": False,  "protected": False, "environment_scope": "*"}
        # Shared registry vars
        variables["REGISTRY_HOST"] = {"value": ctx["registry_host"], "masked": False, "protected": False, "environment_scope": "*"}
        variables["REGISTRY_USER"] = {"value": "",            "masked": True,  "protected": False, "environment_scope": "*"}
        variables["REGISTRY_PASS"] = {"value": "",            "masked": True,  "protected": False, "environment_scope": "*"}
        vars_content = {"project": ctx["gitlab_project"], "scope": "*", "variables": variables}
        vars_yml.write_text(_yaml.dump(vars_content, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        typer.secho(f"  Created .deploylane/vars.yml (standard placeholders)", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  Skipped .deploylane/vars.yml — already exists", fg=typer.colors.YELLOW)

    # ── GitLab CI template (.deploylane/ci/.gitlab-ci.yml) ───────────────────
    ci_dir = base / "ci"
    ci_dir.mkdir(parents=True, exist_ok=True)
    ci_dst = ci_dir / ".gitlab-ci.yml"
    if not ci_dst.exists() or force:
        ci_dst.write_text(render_gitlab_ci(ctx, targets_raw, strategy_top), encoding="utf-8")
        typer.secho(f"  Created .deploylane/ci/.gitlab-ci.yml", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  Skipped .deploylane/ci/.gitlab-ci.yml — already exists", fg=typer.colors.YELLOW)


def project_init(
    strategy: str = typer.Option("plain", "--strategy", help=f"Deployment strategy: {', '.join(VALID_STRATEGIES)}"),
    project: str = typer.Option(..., "--project", help="GitLab path_with_namespace (e.g. acme/backend)"),
    app_name: Optional[str] = typer.Option(None, "--app-name", help="App name (default: last segment of project)"),
    target: str = typer.Option("prod", "--target", help="Initial target name"),
    host: str = typer.Option("", "--host", help="Target server IP or hostname"),
    user: str = typer.Option("deploy", "--user", help="SSH user on target server"),
    deploy_dir: str = typer.Option("", "--deploy-dir", help="Deploy directory on target server"),
    registry_host: str = typer.Option("", "--registry-host", help="Docker registry host"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing deploy.yml / vars.yml"),
    dir: Optional[Path] = typer.Option(None, "--dir", help="Target directory (default: current directory)"),
):
    """
    Scaffold .deploylane/ structure for a new project.

    Creates:
      .deploylane/deploy.yml
      .deploylane/vars.yml
      .deploylane/scripts/deploy.sh
    """
    if not is_valid_strategy(strategy):
        _err(f"Unknown strategy '{strategy}'. Valid: {', '.join(VALID_STRATEGIES)}")

    if dir is not None:
        dir.mkdir(parents=True, exist_ok=True)
        _run_init_in_dir(
            dir, strategy=strategy, project=project, app_name=app_name, target=target,
            host=host, user=user, deploy_dir=deploy_dir, registry_host=registry_host,
            force=force,
        )
        return

    _do_init(
        strategy=strategy, project=project, app_name=app_name, target=target,
        host=host, user=user, deploy_dir=deploy_dir, registry_host=registry_host,
        force=force,
    )
