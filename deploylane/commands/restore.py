from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
import yaml

from ..workspace import load_workspace, project_deploy_yml, project_vars_yml
from ..auth import get_provider
from ..providers.base import ProviderError
from ..ymlvars import read_vars_file, write_vars_file
from ._utils import _err, get_workspace_profile_or_exit
from .workspace._utils import _resolve_ws


# ─── Step helpers ─────────────────────────────────────────────────────────────

def _header(n: int, total: int, title: str) -> None:
    typer.echo("")
    typer.secho(f"  Step {n}/{total}  {title}", bold=True)
    typer.secho("  " + "─" * 50, fg=typer.colors.BRIGHT_BLACK)


def _ok(msg: str) -> None:
    typer.secho(f"  ✓  {msg}", fg=typer.colors.GREEN)


def _warn(msg: str) -> None:
    typer.secho(f"  ⚠  {msg}", fg=typer.colors.YELLOW)


def _fail(msg: str) -> None:
    typer.secho(f"  ✗  {msg}", fg=typer.colors.RED)


# ─── Step 1: Pull GitLab CI variables → vars.yml ──────────────────────────────

def _step_vars(name: str, ws_path: Path) -> bool:
    """Fetch CI variables from GitLab and write to vars.yml. Returns True on success."""
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _fail(f"Project '{name}' not found in workspace.")
        return False

    vars_path = project_vars_yml(project, ws_path.parent)
    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)

    try:
        prj = provider.get_project(project.gitlab_project)
        vars_list = provider.list_variables(prj.id)
    except ProviderError as exc:
        _fail(f"GitLab API error: {exc}")
        return False

    if not vars_list:
        _warn("No CI variables found in GitLab.")
        return True

    # Preserve existing masked values (API never returns them)
    existing: Dict[str, Any] = {}
    if vars_path.exists():
        try:
            existing = read_vars_file(vars_path).get("variables") or {}
        except Exception:
            pass

    variables: Dict[str, Any] = {}
    masked_missing: List[str] = []

    for v in vars_list:
        env_scope = str(getattr(v, "environment_scope", "*") or "*").strip() or "*"
        if v.masked and v.value is None:
            existing_meta = existing.get(v.key) or {}
            preserved = str(existing_meta.get("value", "")).strip() if isinstance(existing_meta, dict) else ""
            if not preserved:
                masked_missing.append(v.key)
            value = preserved
        else:
            value = v.value or ""

        variables[v.key] = {
            "value": value,
            "masked": bool(v.masked),
            "protected": bool(v.protected),
            "environment_scope": env_scope,
        }

    data = {"project": project.gitlab_project, "scope": "*", "variables": variables}
    vars_path.parent.mkdir(parents=True, exist_ok=True)
    write_vars_file(vars_path, data)

    _ok(f"Fetched {len(variables)} variables → {vars_path.relative_to(ws_path.parent)}")
    if masked_missing:
        _warn(f"{len(masked_missing)} masked variable(s) have no value "
              f"(fill manually): {', '.join(sorted(masked_missing))}")

    return True


# ─── Step 2: Sync vars.yml → deploy.yml ───────────────────────────────────────

def _step_sync(name: str, ws_path: Path) -> bool:
    """Run scaffold/sync to propagate vars.yml values into deploy.yml."""
    try:
        from .workspace.init import _do_scaffold
        _do_scaffold(name, ws_path)
        _ok("deploy.yml and infra files updated from vars.yml")
        return True
    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"Sync failed: {exc}")
        return False


# ─── Step 3: Pull server files → compose + .env ───────────────────────────────

def _step_deploy_pull(name: str, ws_path: Path, yes: bool) -> bool:
    """Pull compose + .env from each target server."""
    from .deploy import _pull_target

    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _fail(f"Project '{name}' not found in workspace.")
        return False

    ws_dir = ws_path.parent
    deploy_yml = project_deploy_yml(project, ws_dir)
    if not deploy_yml.exists():
        _warn("deploy.yml not found — skipping server pull.")
        return True

    raw = yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    targets_raw = raw.get("targets") or {}
    strategy_top = str(raw.get("strategy") or "plain").strip()
    app_name = name.replace("-", "_")
    base = (ws_dir / project.path).resolve() / ".deploylane"

    any_failed = False
    # Collect (target → (actual_strategy, actual_compose_file)) from each pull
    server_facts: Dict[str, Any] = {}
    for t_name, t_raw in targets_raw.items():
        if not isinstance(t_raw, dict):
            continue
        host = str(t_raw.get("host") or "").strip()
        if not host:
            _warn(f"[{t_name}] host empty — skipped")
            continue
        try:
            result = _pull_target(name, t_name, t_raw, strategy_top, base, app_name, yes=yes)
            if result:
                server_facts[t_name] = result
        except SystemExit:
            raise
        except Exception as exc:
            _fail(f"[{t_name}] Pull failed: {exc}")
            any_failed = True

    # Reconcile deploy.yml if server reported different strategy, compose_file or ports
    if server_facts:
        _reconcile_deploy_yml(deploy_yml, raw, strategy_top, server_facts, base)

    return not any_failed


def _reconcile_deploy_yml(
    deploy_yml: Path,
    raw: dict,
    strategy_top: str,
    server_facts: Dict[str, Any],
    base: Path,
) -> None:
    """Update deploy.yml targets in-place from server state.

    Updates: strategy, compose_file, ports.blue/green (from APP_PORT_BLUE/GREEN in .env).
    """
    targets = raw.get("targets") or {}
    changed_targets: List[str] = []

    for t_name, (actual_strategy, actual_compose) in server_facts.items():
        t_data = targets.get(t_name)
        if not isinstance(t_data, dict):
            continue

        local_strategy = str(t_data.get("strategy") or strategy_top).strip()
        local_compose  = str(t_data.get("compose_file") or "docker-compose.yml").strip()

        updated = False
        if actual_strategy and actual_strategy != local_strategy:
            t_data["strategy"] = actual_strategy
            updated = True
        if actual_compose and actual_compose != local_compose:
            t_data["compose_file"] = actual_compose
            updated = True

        # Read APP_PORT_BLUE / APP_PORT_GREEN from pulled .env and update ports
        env_file = base / "env" / f"{t_name}.env"
        if env_file.exists():
            env_kvs: Dict[str, str] = {}
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env_kvs[k.strip()] = v.strip()

            port_blue  = env_kvs.get("APP_PORT_BLUE", "")
            port_green = env_kvs.get("APP_PORT_GREEN", "")
            if port_blue or port_green:
                ports = dict(t_data.get("ports") or {})
                if port_blue and str(ports.get("blue", "")) != port_blue:
                    ports["blue"] = int(port_blue) if port_blue.isdigit() else port_blue
                    updated = True
                if port_green and str(ports.get("green", "")) != port_green:
                    ports["green"] = int(port_green) if port_green.isdigit() else port_green
                    updated = True
                if updated:
                    t_data["ports"] = ports

        if updated:
            targets[t_name] = t_data
            changed_targets.append(t_name)

    if not changed_targets:
        return

    # Update top-level strategy to reflect the default_target's strategy
    all_strategies = {str(t.get("strategy") or strategy_top) for t in targets.values() if isinstance(t, dict)}
    if len(all_strategies) == 1:
        raw["strategy"] = list(all_strategies)[0]
    else:
        # Mixed strategies: set top-level to default_target's strategy
        default_target = str(raw.get("default_target") or "").strip()
        if default_target and default_target in targets:
            dt = targets[default_target]
            if isinstance(dt, dict) and dt.get("strategy"):
                raw["strategy"] = dt["strategy"]

    raw["targets"] = targets
    deploy_yml.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    _ok(f"deploy.yml updated from server state (targets: {', '.join(changed_targets)})")


# ─── Step 4: Pull .gitlab-ci.yml from GitLab repo ────────────────────────────

def _step_ci_pull(name: str, ws_path: Path, yes: bool) -> bool:
    """Fetch .gitlab-ci.yml from the GitLab repository."""
    from ..gitlab import get_project_by_path, get_repository_file, GitLabError

    CI_FILE = ".gitlab-ci.yml"
    LOCAL_CI = ".deploylane/ci/.gitlab-ci.yml"

    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _fail(f"Project '{name}' not found in workspace.")
        return False

    ws_dir = ws_path.parent
    local_ci = (ws_dir / project.path).resolve() / LOCAL_CI
    prof = get_workspace_profile_or_exit(ws)

    try:
        gl_project = get_project_by_path(prof.host, prof.token, project.gitlab_project)
        ref = gl_project.default_branch or "main"
        remote_content = get_repository_file(prof.host, prof.token, gl_project.id, CI_FILE, ref=ref)
    except GitLabError as exc:
        _warn(f"Could not fetch .gitlab-ci.yml: {exc}")
        return True  # non-fatal

    if local_ci.exists():
        local_content = local_ci.read_text(encoding="utf-8")
        if remote_content.strip() == local_content.strip():
            _ok(f".gitlab-ci.yml already up to date")
            return True
        if not yes:
            typer.echo("")
            if not typer.confirm(f"  Overwrite local {LOCAL_CI} with GitLab version?", default=True):
                _warn(".gitlab-ci.yml skipped")
                return True

    local_ci.parent.mkdir(parents=True, exist_ok=True)
    local_ci.write_text(remote_content, encoding="utf-8")
    _ok(f".gitlab-ci.yml pulled from {project.gitlab_project} (ref={ref})")
    return True


# ─── Command ───────────────────────────────────────────────────────────────────

def restore(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Overwrite local files without prompting"),
    skip_vars: bool = typer.Option(False, "--skip-vars", help="Skip GitLab variable fetch (Step 1)"),
    skip_pull: bool = typer.Option(False, "--skip-pull", help="Skip server file pull (Step 2)"),
    skip_ci: bool = typer.Option(False, "--skip-ci", help="Skip .gitlab-ci.yml pull (Step 4)"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Rebuild local workspace from live sources (GitLab + server).

    Order:
      1. Pull CI variables from GitLab → vars.yml
      2. Pull server files (compose + .env) → reconcile deploy.yml (strategy, ports)
      3. Sync vars.yml + reconciled deploy.yml → templates (nginx, scripts, etc.)
      4. Pull .gitlab-ci.yml from GitLab repo
      5. Run local check to verify consistency

    Step 2 runs before Step 3 so the scaffold sees up-to-date strategy/ports
    and generates correct templates without needing another restore run.
    """
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    if not any(p.name == name for p in ws.projects):
        _err(f"Project '{name}' not found in workspace.")

    TOTAL = 5
    results: Dict[str, bool] = {}

    typer.secho(f"▶ RESTORE [{name}]", fg=typer.colors.CYAN, bold=True)

    # Step 1: Pull CI variables from GitLab → vars.yml
    _header(1, TOTAL, "Pull GitLab CI variables → vars.yml")
    if skip_vars:
        _warn("Skipped (--skip-vars)")
        results["vars"] = True
    else:
        results["vars"] = _step_vars(name, ws_path)

    # Step 2: Pull server files → reconcile deploy.yml with real strategy/ports
    _header(2, TOTAL, "Pull server files (compose + .env) → reconcile deploy.yml")
    if skip_pull:
        _warn("Skipped (--skip-pull)")
        results["pull"] = True
    else:
        results["pull"] = _step_deploy_pull(name, ws_path, yes=yes)

    # Step 3: Scaffold — runs AFTER pull so it sees reconciled deploy.yml
    _header(3, TOTAL, "Sync vars.yml → deploy.yml + templates")
    results["sync"] = _step_sync(name, ws_path)

    # Step 4: Pull .gitlab-ci.yml from GitLab repo
    _header(4, TOTAL, "Pull .gitlab-ci.yml from GitLab repo")
    if skip_ci:
        _warn("Skipped (--skip-ci)")
        results["ci"] = True
    else:
        results["ci"] = _step_ci_pull(name, ws_path, yes=yes)

    # Step 5 — local check
    _header(5, TOTAL, "Verify local consistency")
    from .check import (
        _check_deploy_yml, _check_deploy_sh, _check_compose,
        _check_env_files, _check_vars_yml, _check_ci_yml,
        _render_section, Finding,
    )

    ws2 = load_workspace(ws_path)
    project = next(p for p in ws2.projects if p.name == name)
    ws_dir = ws_path.parent
    project_path = (ws_dir / project.path).resolve()
    base = project_path / ".deploylane"
    app_name = name.replace("-", "_")

    findings: list[Finding] = []
    raw = _check_deploy_yml(base, findings)
    if raw:
        _check_deploy_sh(base, findings)
        _check_compose(base, raw, app_name, findings)
        _check_env_files(base, raw, app_name, findings)
        vars_data = _check_vars_yml(base, findings)
        _check_ci_yml(base, vars_data, findings)

    typer.echo("")
    _render_section(findings, "local", "Local")

    # Summary
    failed = [k for k, v in results.items() if not v]
    check_errors = [f for f in findings if f.level == "error"]

    typer.echo("")
    typer.secho("  ── Summary " + "─" * 40, bold=True)
    if not failed and not check_errors:
        typer.secho(f"  ✓  [{name}] Restore complete", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"  Run 'dlane check {name} --remote' to verify server state.")
    else:
        if failed:
            _fail(f"Steps with errors: {', '.join(failed)}")
        if check_errors:
            _fail(f"{len(check_errors)} local consistency error(s) — review output above")
        raise typer.Exit(code=1)
