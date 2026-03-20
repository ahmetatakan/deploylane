from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import typer

from ..auth import get_provider
from ..providers.base import ProviderError
from ..workspace import load_workspace, project_vars_yml
from ..ymlvars import read_vars_file, write_vars_file, demo_template, load_vars_yml, _norm_var, VarSpec, _safe_value
from ._utils import _err, get_workspace_profile_or_exit
from .workspace._utils import _resolve_ws, _ws_vars_file

vars_app = typer.Typer(no_args_is_help=True, help="Manage GitLab variables for workspace projects.")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _norm_scope(x: object, default: str = "*") -> str:
    s = str(x or "").strip()
    return s or (default.strip() or "*")


def _iter_yaml_vars(scope_default: str, variables: Dict[str, Any]):
    """Yield normalized (key, env_scope, meta_dict) from YAML variables mapping."""
    scope_default = _norm_scope(scope_default, "*")
    for key, meta in (variables or {}).items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        k = key.strip()
        if not k:
            continue
        env_scope = _norm_scope(meta.get("environment_scope"), scope_default)
        yield (k, env_scope, meta)


# ─── Commands ─────────────────────────────────────────────────────────────────

@vars_app.command("get")
def vars_get(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Fetch GitLab variables into the project's vars.yml."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    ws_path = _resolve_ws(file)
    vars_path = _ws_vars_file(name, ws_path)
    ws = load_workspace(ws_path)
    project = next(p for p in ws.projects if p.name == name)

    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)
    try:
        prj = provider.get_project(project.gitlab_project)
        vars_list = provider.list_variables(prj.id)
    except ProviderError as e:
        _err(str(e))

    if not vars_list:
        typer.echo("No variables found → writing demo template.")
        data = demo_template(project.gitlab_project)
    else:
        typer.echo(f"[{name}] Exporting {len(vars_list)} variables...")

        # Load existing vars.yml to preserve masked values (API never returns them)
        existing: Dict[str, Any] = {}
        if vars_path.exists():
            try:
                existing = read_vars_file(vars_path).get("variables") or {}
            except Exception:
                pass

        data = {"project": project.gitlab_project, "scope": "*", "variables": {}}
        masked_missing: list = []

        for v in vars_list:
            env_scope = str(getattr(v, "environment_scope", "*") or "*").strip() or "*"

            if v.masked and v.value is None:
                # Preserve existing value if available, otherwise warn
                existing_meta = existing.get(v.key) or {}
                preserved = str(existing_meta.get("value", "")).strip() if isinstance(existing_meta, dict) else ""
                if not preserved:
                    masked_missing.append(v.key)
                value = preserved
            else:
                value = v.value or ""

            data["variables"][v.key] = {
                "value": value,
                "masked": bool(v.masked),
                "protected": bool(v.protected),
                "environment_scope": env_scope,
            }

        if masked_missing:
            typer.secho(
                f"  ⚠ {len(masked_missing)} masked variable(s) have no value (GitLab API never returns masked values):",
                fg=typer.colors.YELLOW,
            )
            for k in masked_missing:
                typer.secho(f"    - {k}  ← fill in vars.yml before running 'vars apply'", fg=typer.colors.YELLOW)

    vars_path.parent.mkdir(parents=True, exist_ok=True)
    write_vars_file(vars_path, data)
    typer.secho(f"[{name}] OK → {vars_path}", fg=typer.colors.GREEN)


@vars_app.command("plan")
def vars_plan(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Show what would be created/updated/pruned for a project. Does NOT apply."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    ws_path = _resolve_ws(file)
    vars_path = _ws_vars_file(name, ws_path)

    if not vars_path.exists():
        _err(f"vars.yml not found: {vars_path}\nRun: dlane vars get {name}")

    ws = load_workspace(ws_path)
    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)
    data = read_vars_file(vars_path)
    project = str(data.get("project") or "").strip()
    scope_default = _norm_scope(data.get("scope"), "*")
    variables = data.get("variables", {})

    try:
        prj = provider.get_project(project)
        current_vars = provider.list_variables(prj.id)
    except ProviderError as e:
        _err(str(e))

    desired_pairs: set = set()
    desired_meta: Dict = {}
    for k, e, meta in _iter_yaml_vars(scope_default, variables):
        desired_pairs.add((k, e))
        desired_meta[(k, e)] = meta

    current_pairs: set = set()
    current_map: Dict = {}
    for v in current_vars:
        k = str(getattr(v, "key", "")).strip()
        e = _norm_scope(getattr(v, "environment_scope", "*"), "*")
        if k:
            current_pairs.add((k, e))
            current_map[(k, e)] = v

    creates = sorted(desired_pairs - current_pairs, key=lambda x: (x[0].lower(), x[1]))
    prunes  = sorted(current_pairs - desired_pairs, key=lambda x: (x[0].lower(), x[1]))

    updates = []
    for pair in sorted(desired_pairs & current_pairs, key=lambda x: (x[0].lower(), x[1])):
        meta = desired_meta.get(pair, {})
        cur = current_map.get(pair)
        cur_masked = bool(getattr(cur, "masked", False))
        if (not cur_masked) and str(meta.get("value", "")) != str(getattr(cur, "value", "") or ""):
            updates.append(pair)
        elif bool(meta.get("masked")) != cur_masked or bool(meta.get("protected")) != bool(getattr(cur, "protected", False)):
            updates.append(pair)

    typer.secho(f"VARS PLAN [{name}]", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Project : {project}")
    typer.echo(f"File    : {vars_path}")
    typer.echo("")

    def _block(title: str, items: list, color: str) -> None:
        typer.secho(f"{title} ({len(items)})", fg=getattr(typer.colors, color), bold=True)
        if not items:
            typer.echo("  -")
        for k, e in items:
            typer.echo(f"  - {k}\tenv={e}")

    _block("Create", creates, "GREEN")
    typer.echo("")
    _block("Update", updates, "YELLOW")
    typer.echo("")
    _block("Prune candidates", prunes, "RED")


@vars_app.command("apply")
def vars_apply(
    name: Optional[str] = typer.Argument(None, help="Project alias (omit for --all)"),
    all_projects: bool = typer.Option(False, "--all", help="Apply for all projects in workspace"),
    strict: bool = typer.Option(False, "--strict", help="Exit code 2 if any variable fails"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Apply vars.yml into GitLab variables. Use --all for every project."""
    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)

    if all_projects:
        targets = ws.projects
    elif name:
        match = next((p for p in ws.projects if p.name == name), None)
        if not match:
            _err(f"Project '{name}' not found in workspace.")
        targets = [match]
    else:
        _err("Provide a project name or --all.")

    total_failed = 0
    for project in sorted(targets, key=lambda p: p.name):
        vars_path = project_vars_yml(project, ws_path.parent)
        if not vars_path.exists():
            typer.secho(f"[{project.name}] SKIP — vars.yml not found: {vars_path}", fg=typer.colors.YELLOW)
            continue

        data = read_vars_file(vars_path)
        gl_project = str(data.get("project") or "").strip()
        scope_default = _norm_scope(data.get("scope"), "*")
        variables = data.get("variables", {})

        try:
            prj = provider.get_project(gl_project)
        except ProviderError as e:
            typer.secho(f"[{project.name}] ERROR — {e}", fg=typer.colors.RED)
            total_failed += 1
            continue

        desired_items = sorted(_iter_yaml_vars(scope_default, variables), key=lambda x: (x[0].lower(), x[1]))
        typer.secho(f"[{project.name}] Applying {len(desired_items)} variables...", bold=True)

        failed = 0
        for key, env_scope, meta in desired_items:
            value = str(meta.get("value", ""))
            is_masked = bool(meta.get("masked", False))

            if is_masked and not value.strip():
                typer.secho(
                    f"  SKIP {key}\tenv={env_scope}  (masked, value is empty — fill in vars.yml first)",
                    fg=typer.colors.YELLOW,
                )
                continue

            try:
                provider.set_variable(
                    project_id=prj.id,
                    key=key, value=value,
                    masked=is_masked,
                    protected=bool(meta.get("protected", False)),
                    environment_scope=env_scope,
                    variable_type=str(meta.get("variable_type", "env_var") or "env_var").strip() or "env_var",
                )
                typer.echo(f"  OK  {key}\tenv={env_scope}")
            except ProviderError as e:
                failed += 1
                typer.echo(f"  FAIL {key}\tenv={env_scope}: {e}")

        if failed:
            typer.secho(f"[{project.name}] DONE with {failed} failure(s)", fg=typer.colors.YELLOW)
            total_failed += failed
        else:
            typer.secho(f"[{project.name}] DONE", fg=typer.colors.GREEN)
        typer.echo("")

    if total_failed and strict:
        raise typer.Exit(code=2)


@vars_app.command("diff")
def vars_diff(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    scope: str = typer.Option("*", "--scope", help="Environment scope (default: *)"),
    show_values: bool = typer.Option(False, "--show-values", help="Print values (careful: secrets)"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Show diff between GitLab variables and local vars.yml for a project."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)
    ws_path = _resolve_ws(file)
    vars_path = _ws_vars_file(name, ws_path)

    if not vars_path.exists():
        _err(f"vars.yml not found: {vars_path}\nRun: dlane vars get {name}")

    ws = load_workspace(ws_path)
    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)
    scope = str(scope or "*").strip() or "*"

    try:
        local_doc = load_vars_yml(vars_path)
    except Exception as e:
        _err(f"Failed to read vars.yml: {e}")

    project = str(local_doc.get("project") or "").strip()
    local_vars_raw = local_doc.get("variables") or {}

    local_vars: Dict = {}
    for k, v in local_vars_raw.items():
        if not isinstance(v, dict):
            continue
        key = str(k).strip()
        if not key:
            continue
        spec = _norm_var(v)
        if spec.environment_scope != scope:
            continue
        local_vars[(key, spec.environment_scope)] = spec

    try:
        prj = provider.get_project(project)
        remote_items = provider.list_variables(prj.id)
    except ProviderError as e:
        _err(str(e))

    remote_vars: Dict = {}
    for it in remote_items:
        key = str(getattr(it, "key", "")).strip()
        if not key:
            continue
        env_scope = str(getattr(it, "environment_scope", "*") or "*").strip() or "*"
        if env_scope != scope:
            continue
        remote_vars[(key, env_scope)] = VarSpec(
            value=str(getattr(it, "value", "") or ""),
            masked=bool(getattr(it, "masked", False)),
            protected=bool(getattr(it, "protected", False)),
            environment_scope=env_scope,
        )

    added   = sorted(set(local_vars) - set(remote_vars), key=lambda x: (x[0].lower(), x[1]))
    removed = sorted(set(remote_vars) - set(local_vars), key=lambda x: (x[0].lower(), x[1]))
    changed = [
        (p, remote_vars[p], local_vars[p])
        for p in sorted(set(local_vars) & set(remote_vars), key=lambda x: (x[0].lower(), x[1]))
        if remote_vars[p] != local_vars[p]
    ]

    typer.secho(f"VARS DIFF [{name}]", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Project: {project}")
    typer.echo(f"Scope  : {scope}")
    typer.echo(f"File   : {vars_path}")
    typer.echo("")

    if not (added or removed or changed):
        typer.secho("No changes.", fg=typer.colors.GREEN)
        return

    if added:
        typer.secho(f"+ Added ({len(added)}):", fg=typer.colors.GREEN, bold=True)
        for k, e in added:
            typer.echo(f"  + {k}\tenv={e}\t= {_safe_value(k, local_vars[(k,e)], show_values)}")
        typer.echo("")

    if removed:
        typer.secho(f"- Removed ({len(removed)}):", fg=typer.colors.RED, bold=True)
        for k, e in removed:
            typer.echo(f"  - {k}\tenv={e}\t= {_safe_value(k, remote_vars[(k,e)], show_values)}")
        typer.echo("")

    if changed:
        typer.secho(f"~ Changed ({len(changed)}):", fg=typer.colors.YELLOW, bold=True)
        for (k, e), old, new in changed:
            typer.echo(f"  ~ {k}\tenv={e}")
            if _safe_value(k, old, show_values) != _safe_value(k, new, show_values):
                typer.echo(f"    - {_safe_value(k, old, show_values)}")
                typer.echo(f"    + {_safe_value(k, new, show_values)}")
        typer.echo("")

    raise typer.Exit(code=2)


@vars_app.command("prune")
def vars_prune(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
):
    """Delete GitLab variables that are not in vars.yml."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    vars_path = _ws_vars_file(name, ws_path)

    if not vars_path.exists():
        _err(f"vars.yml not found: {vars_path}\nRun: dlane vars get {name}")

    ws = load_workspace(ws_path)
    prof = get_workspace_profile_or_exit(ws)
    provider = get_provider(prof)
    data = read_vars_file(vars_path)
    project = str(data.get("project") or "").strip()
    scope_default = _norm_scope(data.get("scope"), "*")
    variables = data.get("variables", {})

    try:
        prj = provider.get_project(project)
        current_vars = provider.list_variables(prj.id)
    except ProviderError as e:
        _err(str(e))

    desired_pairs: set = set()
    for k, e, _ in _iter_yaml_vars(scope_default, variables):
        desired_pairs.add((k, e))

    orphans = sorted(
        [
            (str(getattr(v, "key", "")).strip(), _norm_scope(getattr(v, "environment_scope", "*"), "*"))
            for v in current_vars
            if (str(getattr(v, "key", "")).strip(), _norm_scope(getattr(v, "environment_scope", "*"), "*")) not in desired_pairs
            and str(getattr(v, "key", "")).strip()
        ],
        key=lambda x: (x[0].lower(), x[1]),
    )

    if not orphans:
        typer.secho(f"[{name}] Nothing to prune — GitLab variables match vars.yml.", fg=typer.colors.GREEN)
        return

    typer.secho(f"[{name}] Variables in GitLab not in vars.yml ({len(orphans)}):", fg=typer.colors.RED, bold=True)
    for k, e in orphans:
        typer.echo(f"  - {k}\tenv={e}")
    typer.echo("")

    if not yes:
        typer.confirm(
            f"  Delete {len(orphans)} variable(s) from GitLab? This cannot be undone.",
            abort=True,
        )

    failed = 0
    for k, e in orphans:
        try:
            provider.delete_variable(project_id=prj.id, key=k, environment_scope=e)
            typer.echo(f"  DEL {k}\tenv={e}")
        except ProviderError as ex:
            typer.secho(f"  FAIL {k}\tenv={e}: {ex}", fg=typer.colors.RED)
            failed += 1

    if failed:
        typer.secho(f"[{name}] Done with {failed} failure(s).", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    else:
        typer.secho(f"[{name}] Pruned {len(orphans)} variable(s).", fg=typer.colors.GREEN)
