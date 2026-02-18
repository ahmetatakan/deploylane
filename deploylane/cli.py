from __future__ import annotations
from typing import Dict, Optional, Tuple, Any, List, Set
import typer
import sys
from datetime import datetime
from typing import Optional
from typer.main import get_command
from pathlib import Path
from deploylane.deployspec import load_deployspec, write_env_file
from .deployproof import file_entry, write_json_deterministic, sha256_file, sha256_string
from .remote import copy_dir, RemoteError, ssh_run
from .auth import (
    login as do_login,
    logout as do_logout,
    load_active_profile,
    status as do_status,
    AuthError,
)
from .gitlab import (
    whoami as gl_whoami, 
    GitLabError, 
    list_projects, 
    get_project_by_path, 
    list_project_variables, 
    set_project_variable,
    delete_project_variable
)
from .config import (
    Profile,
    DEFAULT_HOST,
    config_path,
    load_config,
    get_active_profile_name,
    env_fallback_host,
    env_fallback_token,
    normalize_host,
    get_profile,
    set_active_profile_name,
    save_config
)
from .ymlvars import (
    write_vars_file, 
    demo_template, 
    read_vars_file, 
    save_vars_yml, 
    load_vars_yml, 
    _norm_var, 
    VarSpec, 
    _safe_value, 
    DEFAULT_FILE
)

DEPLOY_YML_DEFAULT = Path(".deploylane/deploy.yml")
VARS_YML_DEFAULT = Path(".deploylane/vars.yml")
RENDER_DIR_DEFAULT = Path(".deploylane/rendered")

app = typer.Typer(add_completion=True, no_args_is_help=True)

config_app = typer.Typer(no_args_is_help=True, help="Config helpers (debug).")
app.add_typer(config_app, name="config")

project_app = typer.Typer(no_args_is_help=True, help="Project helpers.")
app.add_typer(project_app, name="project")

vars_app = typer.Typer(no_args_is_help=True, help="Manage GitLab project variables via YAML.")
app.add_typer(vars_app, name="vars")

profile_app = typer.Typer(no_args_is_help=True, help="Manage local profiles.")
app.add_typer(profile_app, name="profile")

deploy_app = typer.Typer(no_args_is_help=True, help="Deployment helpers (render plans, later remote execution).")
app.add_typer(deploy_app, name="deploy")

def _desired_pairs_from_yml(scope_default: str, variables: Dict[str, Any]) -> Set[Tuple[str, str]]:
    out: Set[Tuple[str, str]] = set()
    for key, meta in variables.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        env_scope = meta.get("environment_scope", scope_default)
        env_scope = str(env_scope or "*").strip() or "*"
        out.add((key.strip(), env_scope))
    return out

@app.callback(invoke_without_command=False)
def _global_callback(ctx: typer.Context) -> None:
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

def get_active_profile_or_exit() -> Profile:
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)

    if not prof:
        _err("Not logged in. Run: dlane login")

    return prof

def _show_command_help_and_exit(ctx: typer.Context) -> None:
    """
    Print help for the current command and exit.
    Keeps UX consistent when required options are missing.
    """
    typer.echo(ctx.get_help())
    raise typer.Exit(code=0)

def _err(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


@app.command()
def login(
    host: Optional[str] = typer.Option(None, "--host", help="GitLab host (e.g. https://gitlab.com)"),
    token: Optional[str] = typer.Option(None, "--token", help="GitLab Personal Access Token (PAT)"),
    profile: str = typer.Option("default", "--profile", help="Profile name (stored locally)"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail instead of prompting"),
    registry_host: Optional[str] = typer.Option(
        None, "--registry-host", help="Docker registry host (optional, e.g. registry-gitlab.example.com)"
    ),
):
    """
    Store token locally and verify via GitLab API (GET /api/v4/user).
    """
    if host is None:
        host = env_fallback_host()
    if token is None:
        token = env_fallback_token()

    if non_interactive:
        if not host or not token:
            _err("Missing --host/--token (or env GITLAB_HOST/GITLAB_TOKEN) in non-interactive mode.")
    else:
        if not host:
            host = typer.prompt("GitLab host", default=DEFAULT_HOST)
        if not token:
            token = typer.prompt("GitLab token (PAT)", hide_input=True)

    assert host is not None and token is not None

    try:
        prof, user = do_login(profile_name=profile, host=host, token=token, registry_host=(registry_host or ""))
    except (AuthError, GitLabError) as e:
        _err(str(e))

    typer.secho("Login OK", fg=typer.colors.GREEN)
    typer.echo(f"Profile : {prof.name}")
    typer.echo(f"Host    : {prof.host}")
    typer.echo(f"User    : {getattr(user, 'username', '-')}")
    typer.echo(f"Config  : {config_path()}")


@app.command()
def whoami():
    """Show current user for the active profile (requires login)."""
    try:
        prof = load_active_profile()
        u = gl_whoami(prof.host, prof.token)
    except AuthError as e:
        _err(str(e))
    except GitLabError as e:
        _err(str(e))

    typer.echo(f"{u.username} ({u.name}) @ {prof.host}")


@app.command()
def status():
    """Show login status for the active profile."""
    s = do_status()
    typer.echo(f"config         : {s.get('config_path')}")
    typer.echo(f"active_profile : {s.get('active_profile')}")
    typer.echo(f"source         : {s.get('source')}")
    typer.echo(f"has_profile    : {s.get('has_profile')}")
    typer.echo(f"host           : {s.get('host') or '-'}")
    typer.echo(f"registry_host  : {s.get('registry_host') or '-'}")
    typer.echo(f"logged_in      : {s.get('logged_in')}")
    if s.get("username"):
        typer.echo(f"user           : {s.get('username')} ({s.get('name')})")


@app.command()
def logout(
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile to remove (default: active)"),
    all_profiles: bool = typer.Option(False, "--all", help="Remove all stored profiles"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Do not prompt"),
):
    """Remove stored credentials from the local config."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    target = profile or active

    if all_profiles:
        if not yes and not typer.confirm("Remove ALL stored profiles?"):
            raise typer.Exit(code=0)
        removed = do_logout(profile_name=target, all_profiles=True)
        typer.secho(f"Logged out. Removed profiles: {removed}", fg=typer.colors.GREEN)
        return

    if not yes and not typer.confirm(f"Logout profile '{target}'?"):
        raise typer.Exit(code=0)

    removed = do_logout(profile_name=target, all_profiles=False)
    if removed:
        typer.secho(f"Logged out: {target}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"No such profile: {target}", fg=typer.colors.YELLOW)


@config_app.command('show')
def config_show():
    """Print config path and active profile (debug helper)."""
    cfg = load_config()
    typer.echo(f"path   : {config_path()}")
    typer.echo(f"active : {get_active_profile_name(cfg)}")


@project_app.command("list")
def projects_list(
    search: Optional[str] = typer.Option(None, "--search", help="Search projects by name/path"),
    owned: bool = typer.Option(False, "--owned", help="Only projects owned by the user"),
    membership: bool = typer.Option(True, "--membership/--no-membership", help="Only projects the user is a member of"),
    limit: int = typer.Option(50, "--limit", min=1, max=5000, help="Max number of projects to print (after sorting)"),
):
    """List GitLab projects visible to the active profile."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if prof is None:
        _err(f"Not logged in (active profile '{active}'). Run: dlane login --profile {active}")

    # prof.host, prof.token ile devam
    projects = list_projects(
        host=prof.host,
        token=prof.token,
        search=search,
        owned=owned,
        membership=membership,
    )
    
    if not projects:
        typer.echo("No projects returned.")
        typer.echo("Try: dlane projects-list --no-membership  (or --owned)")
        raise typer.Exit(code=0)

    # Deterministic output
    projects = sorted(projects, key=lambda p: p.path_with_namespace.lower())
    projects = projects[:limit]

    for p in projects:
        typer.echo(
            f"{p.id}\t{p.path_with_namespace}\t"
            f"{p.default_branch or '-'}\t"
            f"{p.web_url or '-'}"
        )

def _norm_scope(x: object, default: str = "*") -> str:
    s = str(x or "").strip()
    return s or (default.strip() or "*")

def _iter_yaml_vars(scope_default: str, variables: Dict[str, Any]):
    """
    Yield normalized (key, env_scope, meta_dict) from YAML variables mapping.
    - env_scope: meta.environment_scope if set else scope_default else '*'
    - key is stripped
    """
    scope_default = _norm_scope(scope_default, "*")
    for key, meta in (variables or {}).items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        k = key.strip()
        if not k:
            continue
        env_scope = _norm_scope(meta.get("environment_scope"), scope_default)
        yield (k, env_scope, meta)

@vars_app.command("get")
def vars_get(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", help="Project path_with_namespace"),
    out: Path = typer.Option(DEFAULT_FILE, "--out", help="Output YAML file"),
):
    """
    Fetch GitLab variables and export them into a YAML file.
    If none exist, generates a demo template.
    """
    if not project:
        _show_command_help_and_exit(ctx)

    prof = get_active_profile_or_exit()

    try:
        prj = get_project_by_path(prof.host, prof.token, project)
        vars_list = list_project_variables(prof.host, prof.token, prj.id)
    except GitLabError as e:
        _err(str(e))

    if not vars_list:
        typer.echo("No variables found → writing demo template.")
        data = demo_template(project)
    else:
        typer.echo(f"Exporting {len(vars_list)} variables...")

        data = {"project": project, "scope": "*", "variables": {}}

        for v in vars_list:
            env_scope = str(getattr(v, "environment_scope", "*") or "*").strip() or "*"
            data["variables"][v.key] = {
                "value": v.value or "",
                "masked": bool(v.masked),
                "protected": bool(v.protected),
                "environment_scope": env_scope,
            }

    write_vars_file(out, data)
    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo(f"Saved → {out}")

@vars_app.command("plan")
def vars_plan(
    ctx: typer.Context,
    file: Path = typer.Option(DEFAULT_FILE, "--file", help="YAML file to plan against GitLab"),
):
    """
    Show a deterministic plan: what would be created/updated, and what exists in GitLab but not in YAML (prune candidates).
    Does NOT apply or delete anything.
    """
    prof = get_active_profile_or_exit()

    if not file.exists():
        _err(f"YAML file not found: {file}")

    data = read_vars_file(file)

    project = str(data.get("project") or "").strip()
    scope_default = _norm_scope(data.get("scope"), "*")
    variables = data.get("variables", {})

    if not project or not isinstance(variables, dict):
        _err("Invalid YAML structure (missing project/variables).")

    try:
        prj = get_project_by_path(prof.host, prof.token, project)
        current_vars = list_project_variables(prof.host, prof.token, prj.id)
    except GitLabError as e:
        _err(str(e))

    # Desired set/map (pair keyed)
    desired_pairs = set()
    desired_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for k, e, meta in _iter_yaml_vars(scope_default, variables):
        desired_pairs.add((k, e))
        desired_meta[(k, e)] = meta

    # Current set/map (pair keyed)
    current_pairs = set()
    current_map: Dict[Tuple[str, str], Any] = {}
    for v in current_vars:
        k = str(getattr(v, "key", "")).strip()
        e = _norm_scope(getattr(v, "environment_scope", "*"), "*")
        if not k:
            continue
        current_pairs.add((k, e))
        current_map[(k, e)] = v

    creates = sorted(desired_pairs - current_pairs, key=lambda x: (x[0].lower(), x[1]))
    prunes = sorted(current_pairs - desired_pairs, key=lambda x: (x[0].lower(), x[1]))

    # Updates: only compare when remote value is readable (not masked)
    updates: List[Tuple[str, str]] = []
    for pair in sorted(desired_pairs & current_pairs, key=lambda x: (x[0].lower(), x[1])):
        meta = desired_meta.get(pair, {})
        desired_val = str(meta.get("value", ""))
        desired_masked = bool(meta.get("masked", False))
        desired_protected = bool(meta.get("protected", False))

        cur = current_map.get(pair)
        cur_masked = bool(getattr(cur, "masked", False))
        cur_protected = bool(getattr(cur, "protected", False))

        # Value compare only if remote is not masked (masked vars usually return empty/placeholder)
        cur_val = str(getattr(cur, "value", "") or "")
        if (not cur_masked) and (desired_val != cur_val):
            updates.append(pair)
            continue

        # Also consider flag changes
        if desired_masked != cur_masked or desired_protected != cur_protected:
            updates.append(pair)
            continue

    typer.secho("VARS PLAN", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Project : {project}")
    typer.echo(f"File    : {file}")
    typer.echo("")

    def _print_block(title: str, items: list[tuple[str, str]], color: str) -> None:
        typer.secho(f"{title} ({len(items)})", fg=getattr(typer.colors, color), bold=True)
        if not items:
            typer.echo("  -")
            return
        for k, e in items:
            typer.echo(f"  - {k}\tenv={e}")

    _print_block("Create", creates, "GREEN")
    typer.echo("")
    _print_block("Update (value/flags differ or unknown)", updates, "YELLOW")
    typer.echo("")
    _print_block("Prune candidates (exists in GitLab, not in YAML)", prunes, "RED")

@vars_app.command("apply")
def vars_set(
    ctx: typer.Context,
    file: Path = typer.Option(DEFAULT_FILE, "--file", help="YAML file to apply"),
    strict: bool = typer.Option(False, "--strict", help="Exit code 2 if any variable fails"),
):
    """
    Apply variables from a YAML file into GitLab project variables.
    """
    prof = get_active_profile_or_exit()

    if not file.exists():
        _err(f"YAML file not found: {file}")

    data = read_vars_file(file)

    project = str(data.get("project") or "").strip()
    scope_default = _norm_scope(data.get("scope"), "*")
    variables = data.get("variables", {})

    if not project or not isinstance(variables, dict):
        _err("Invalid YAML structure (missing project/variables).")

    try:
        prj = get_project_by_path(prof.host, prof.token, project)
    except GitLabError as e:
        _err(str(e))

    # Deterministic list of pairs
    desired_items = sorted(
        [(k, e, meta) for (k, e, meta) in _iter_yaml_vars(scope_default, variables)],
        key=lambda x: (x[0].lower(), x[1]),
    )

    typer.echo(f"Applying {len(desired_items)} variables to {project}...")

    failed = 0

    for key, env_scope, meta in desired_items:
        value = str(meta.get("value", ""))
        masked = bool(meta.get("masked", False))
        protected = bool(meta.get("protected", False))
        variable_type = str(meta.get("variable_type", "env_var") or "env_var").strip() or "env_var"

        try:
            set_project_variable(
                host=prof.host,
                token=prof.token,
                project_id=prj.id,
                key=key,
                value=value,
                masked=masked,
                protected=protected,
                environment_scope=env_scope,
                variable_type=variable_type,
            )
            typer.echo(f"  OK  {key}\tenv={env_scope}")
        except GitLabError as e:
            failed += 1
            typer.echo(f"  FAIL {key}\tenv={env_scope}: {e}")

    if failed:
        typer.secho(f"DONE with failures: {failed}", fg=typer.colors.YELLOW)
        if strict:
            raise typer.Exit(code=2)
        return

    typer.secho("DONE", fg=typer.colors.GREEN)
    
@vars_app.command("prune")
def vars_prune(
    ctx: typer.Context,
    file: Path = typer.Option(DEFAULT_FILE, "--file", help="YAML file to use as source of truth"),
    yes: bool = typer.Option(False, "--yes", help="Actually delete (otherwise only prints the plan)"),
    env_scope: Optional[str] = typer.Option(
        None,
        "--env-scope",
        help="Only prune variables for this environment_scope (example: '*', 'production')",
    ),
):
    """
    Delete GitLab variables that exist in GitLab but are NOT present in YAML.
    By default this is a dry-run unless --yes is provided.
    """
    prof = get_active_profile_or_exit()

    if not file.exists():
        _err(f"YAML file not found: {file}")

    data = read_vars_file(file)

    project = str(data.get("project") or "").strip()
    scope_default = _norm_scope(data.get("scope"), "*")
    variables = data.get("variables", {})

    if not project or not isinstance(variables, dict):
        _err("Invalid YAML structure (missing project/variables).")

    try:
        prj = get_project_by_path(prof.host, prof.token, project)
        current_vars = list_project_variables(prof.host, prof.token, prj.id)
    except GitLabError as e:
        _err(str(e))

    desired_pairs = set()
    for k, e, _meta in _iter_yaml_vars(scope_default, variables):
        desired_pairs.add((k, e))

    current_pairs = set()
    for v in current_vars:
        k = str(getattr(v, "key", "")).strip()
        e = _norm_scope(getattr(v, "environment_scope", "*"), "*")
        if not k:
            continue
        current_pairs.add((k, e))

    prunes = sorted(current_pairs - desired_pairs, key=lambda x: (x[0].lower(), x[1]))

    if env_scope is not None:
        env_scope = _norm_scope(env_scope, "*")
        prunes = [pe for pe in prunes if pe[1] == env_scope]

    typer.secho("VARS PRUNE", fg=typer.colors.RED, bold=True)
    typer.echo(f"Project : {project}")
    typer.echo(f"File    : {file}")
    if env_scope is not None:
        typer.echo(f"Filter  : env_scope={env_scope}")
    typer.echo("")

    if not prunes:
        typer.secho("Nothing to prune.", fg=typer.colors.GREEN)
        return

    typer.secho(f"Prune candidates ({len(prunes)})", fg=typer.colors.YELLOW, bold=True)
    for k, e in prunes:
        typer.echo(f"  - {k}\tenv={e}")

    if not yes:
        typer.echo("")
        typer.secho("Dry-run only. Re-run with --yes to delete.", fg=typer.colors.CYAN)
        return

    typer.echo("")
    typer.secho("Deleting...", fg=typer.colors.RED, bold=True)

    failed = 0
    for k, e in prunes:
        try:
            delete_project_variable(
                host=prof.host,
                token=prof.token,
                project_id=prj.id,
                key=k,
                environment_scope=e,
            )
            typer.echo(f"  OK   {k}\tenv={e}")
        except GitLabError as ex:
            failed += 1
            typer.echo(f"  FAIL {k}\tenv={e}: {ex}")

    if failed:
        typer.secho(f"DONE with failures: {failed}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=2)

    typer.secho("DONE", fg=typer.colors.GREEN)

@vars_app.command("diff")
def vars_diff(
    project: Optional[str] = typer.Option(None, "--project", help="Project path_with_namespace (e.g. group/repo). If omitted, read from vars.yml"),
    file: Optional[Path] = typer.Option(None, "--file", help="YAML file path (default: .deploylane/vars.yml)"),
    scope: str = typer.Option("*", "--scope", help="Environment scope to compare (default: *)"),
    show_values: bool = typer.Option(False, "--show-values", help="Print values in diff (careful: secrets)"),
    exit_code: bool = typer.Option(True, "--exit-code/--no-exit-code", help="Exit with code 2 if changes exist"),
):
    """
    Show diff between GitLab variables and local vars.yml (deterministic ordering).
    Diff keying is (key, environment_scope).
    """
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if not prof:
        _err("Not logged in. Run: dlane login")

    if file is None:
        file = Path(".deploylane") / "vars.yml"

    try:
        local_doc = load_vars_yml(file)
    except Exception as e:
        _err(f"failed to read vars yaml: {e}")

    local_project = str(local_doc.get("project") or "").strip()
    if not project:
        if not local_project:
            _err("Missing --project and vars.yml has no 'project'.")
        project = local_project

    if local_project and local_project != project:
        _err(f"vars.yml project mismatch: file has '{local_project}', you passed '{project}'")

    local_vars_raw = local_doc.get("variables") or {}
    if not isinstance(local_vars_raw, dict):
        _err("Invalid vars.yml: 'variables' must be a mapping")

    scope = str(scope or "*").strip() or "*"

    # Local: (key, env_scope) -> VarSpec
    local_vars: Dict[Tuple[str, str], VarSpec] = {}
    for k, v in local_vars_raw.items():
        if not isinstance(v, dict):
            continue
        key = str(k).strip()
        if not key:
            continue
        spec = _norm_var(v)
        env_scope = spec.environment_scope
        if env_scope != scope:
            continue
        local_vars[(key, env_scope)] = spec

    # Remote: fetch + filter
    try:
        prj = get_project_by_path(prof.host, prof.token, project)
        remote_items = list_project_variables(prof.host, prof.token, prj.id)
    except Exception as e:
        _err(str(e))

    remote_vars: Dict[Tuple[str, str], VarSpec] = {}
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

    local_keys = set(local_vars.keys())
    remote_keys = set(remote_vars.keys())

    added = sorted(local_keys - remote_keys, key=lambda x: (x[0].lower(), x[1]))
    removed = sorted(remote_keys - local_keys, key=lambda x: (x[0].lower(), x[1]))
    common = sorted(local_keys & remote_keys, key=lambda x: (x[0].lower(), x[1]))

    changed: List[Tuple[Tuple[str, str], VarSpec, VarSpec]] = []
    for pair in common:
        old = remote_vars[pair]
        new = local_vars[pair]
        if old != new:
            changed.append((pair, old, new))

    has_changes = bool(added or removed or changed)

    typer.echo(f"Project: {project}")
    typer.echo(f"Scope  : {scope}")
    typer.echo(f"File   : {file}")
    typer.echo("")

    if not has_changes:
        typer.secho("No changes.", fg=typer.colors.GREEN)
        return

    if added:
        typer.secho(f"+ Added ({len(added)}):", fg=typer.colors.GREEN, bold=True)
        for (k, e) in added:
            spec = local_vars[(k, e)]
            val = _safe_value(k, spec, show_values)
            typer.echo(f"  + {k}\tenv={e}\t= {val}\tmasked={spec.masked} protected={spec.protected}")
        typer.echo("")

    if removed:
        typer.secho(f"- Removed ({len(removed)}):", fg=typer.colors.RED, bold=True)
        for (k, e) in removed:
            spec = remote_vars[(k, e)]
            val = _safe_value(k, spec, show_values)
            typer.echo(f"  - {k}\tenv={e}\t= {val}\tmasked={spec.masked} protected={spec.protected}")
        typer.echo("")

    if changed:
        typer.secho(f"~ Changed ({len(changed)}):", fg=typer.colors.YELLOW, bold=True)
        for (k, e), old, new in sorted(changed, key=lambda x: (x[0][0].lower(), x[0][1])):
            oldv = _safe_value(k, old, show_values)
            newv = _safe_value(k, new, show_values)
            typer.echo(f"  ~ {k}\tenv={e}")
            if oldv != newv:
                typer.echo(f"    - value: {oldv}")
                typer.echo(f"    + value: {newv}")
            if old.masked != new.masked:
                typer.echo(f"    - masked: {old.masked}")
                typer.echo(f"    + masked: {new.masked}")
            if old.protected != new.protected:
                typer.echo(f"    - protected: {old.protected}")
                typer.echo(f"    + protected: {new.protected}")
        typer.echo("")

    if exit_code:
        raise typer.Exit(code=2)

@profile_app.command("list")
def profiles_list() -> None:
    """List stored profiles and show the active one."""
    cfg = load_config()
    active = get_active_profile_name(cfg)

    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        typer.echo("No profiles found. Run: dlane login")
        raise typer.Exit(code=1)

    # Deterministic ordering
    for name in sorted(profiles.keys()):
        mark = "*" if name == active else " "
        p = profiles.get(name, {})
        host = p.get("host", "")
        typer.echo(f"{mark} {name}\t{host}")

    typer.echo("")
    typer.echo(f"Active profile: {active}")

@profile_app.command("use")
def profile_use(
    profile: Optional[str] = typer.Argument(
        None,
        help="Profile name to activate",
        show_default=False,
    ),
):
    cfg = load_config()

    # If not provided, prompt user
    if not profile:
        # List profiles to help selection
        profiles = cfg.get("profiles", {})
        names = sorted(profiles.keys()) if isinstance(profiles, dict) else []

        if not names:
            typer.secho("No profiles found. Run `dlane login --profile <name>` first.", fg=typer.colors.RED)
            raise typer.Exit(1)

        typer.echo("Available profiles:")
        for n in names:
            typer.echo(f"  - {n}")

        profile = typer.prompt("Profile to activate")

    # Validate existence
    prof = get_profile(cfg, profile)
    if not prof:
        typer.secho(f"Profile not found: {profile}", fg=typer.colors.RED)
        raise typer.Exit(1)

    set_active_profile_name(cfg, profile)
    save_config(cfg)

    typer.secho("Active profile updated.", fg=typer.colors.GREEN)
    typer.echo(f"active : {profile}")
    typer.echo(f"host   : {prof.host}")

@deploy_app.command("render")
def deploy_render(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Target name (defaults: deploy.yml default_target or single target)"
    ),
    deploy_file: Path = typer.Option(DEPLOY_YML_DEFAULT, "--deploy-file", help="Deploy spec YAML path"),
    vars_file: Path = typer.Option(VARS_YML_DEFAULT, "--vars-file", help="Vars YAML path"),
    out_dir: Path = typer.Option(RENDER_DIR_DEFAULT, "--out-dir", help="Output base directory"),
):
    """
    Render a deterministic .env for a target using local YAML files only.
    Output:
      .deploylane/rendered/<target>/.env
    """
    if not deploy_file.exists():
        _err(f"deploy.yml not found: {deploy_file}")

    if not vars_file.exists():
        _err(f"vars.yml not found: {vars_file} (run: dlane vars get --project <path>)")

    try:
        spec = load_deployspec(deploy_file)
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    # Resolve target deterministically
    resolved_target = target
    if not resolved_target:
        if getattr(spec, "default_target", None):
            resolved_target = spec.default_target
        elif len(spec.targets) == 1:
            resolved_target = next(iter(spec.targets.keys()))

    if not resolved_target:
        typer.echo("Missing --target. Available targets:")
        for name in sorted(spec.targets.keys()):
            typer.echo(f"  - {name}")
        typer.echo("")
        typer.echo("Tip: set 'default_target' in .deploylane/deploy.yml or pass --target <name>.")
        raise typer.Exit(code=1)

    if resolved_target not in spec.targets:
        available = ", ".join(sorted(spec.targets.keys()))
        _err(f"Unknown target '{resolved_target}'. Available: {available}")

    t = spec.targets[resolved_target]

    # Reuse your existing vars.yml loader
    data = read_vars_file(vars_file)

    project = data.get("project")
    variables = data.get("variables", {})
    if not isinstance(project, str) or not isinstance(variables, dict):
        _err("Invalid vars.yml structure (missing project/variables).")

    if project.strip() != spec.project.strip():
        _err(
            "Project mismatch:\n"
            f"  deploy.yml project: {spec.project}\n"
            f"  vars.yml   project: {project}\n"
            "Refusing to render to avoid mistakes."
        )

    # Build env map deterministically from vars.yml values (local-only)
    env: Dict[str, str] = {}

    for key, meta in variables.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue
        val = meta.get("value", "")
        env[key] = "" if val is None else str(val)

    # Add deterministic target hints
    env.setdefault("DLANE_TARGET", resolved_target)

    out_path = out_dir / resolved_target / ".env"
    written, sanitized = write_env_file(out_path, env)
    typer.echo(f"Vars    : {written} keys")
    if sanitized:
        typer.secho(f"Note: {sanitized} values sanitized for .env", fg=typer.colors.YELLOW)

    typer.secho("Render OK", fg=typer.colors.GREEN)
    typer.echo(f"Project : {spec.project}")
    typer.echo(f"Target  : {resolved_target}")
    typer.echo(f"Output  : {out_path}")
    typer.echo(f"Vars    : {written} keys")
    if sanitized:
        typer.secho(
            f"Note: {sanitized} values contained newlines and were sanitized as \\n for .env compatibility.",
            fg=typer.colors.YELLOW,
        )

@deploy_app.command("plan")
def deploy_plan(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Target name (defaults: deploy.yml default_target or single target)"
    ),
    deploy_file: Path = typer.Option(DEPLOY_YML_DEFAULT, "--deploy-file", help="Deploy spec YAML path"),
    vars_file: Path = typer.Option(VARS_YML_DEFAULT, "--vars-file", help="Vars YAML path"),
    out_dir: Path = typer.Option(RENDER_DIR_DEFAULT, "--out-dir", help="Output base directory"),
    script: Path = typer.Option(Path(".deploylane/scripts/deploy.sh"), "--script", help="Deploy script path (server-side)"),
):
    """
    Print a deterministic deployment plan (no execution).
    Uses only local YAML files.
    """
    if not deploy_file.exists():
        _err(f"deploy.yml not found: {deploy_file}")
    if not vars_file.exists():
        _err(f"vars.yml not found: {vars_file} (run: dlane vars get --project <path>)")

    try:
        spec = load_deployspec(deploy_file)
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    # Resolve target deterministically
    resolved_target = target
    if not resolved_target:
        if getattr(spec, "default_target", None):
            resolved_target = spec.default_target
        elif len(spec.targets) == 1:
            resolved_target = next(iter(spec.targets.keys()))

    if not resolved_target:
        typer.echo("Missing --target. Available targets:")
        for name in sorted(spec.targets.keys()):
            typer.echo(f"  - {name}")
        typer.echo("")
        typer.echo("Tip: set 'default_target' in .deploylane/deploy.yml or pass --target <name>.")
        raise typer.Exit(code=1)

    if resolved_target not in spec.targets:
        available = ", ".join(sorted(spec.targets.keys()))
        _err(f"Unknown target '{resolved_target}'. Available: {available}")

    t = spec.targets[resolved_target]
    target_scope = (getattr(t, "env_scope", "*") or "*").strip() or "*"
    strategy = (getattr(t, "strategy", "plain") or "plain").strip() or "plain"

    # Load vars.yml
    data = read_vars_file(vars_file)
    project = data.get("project")
    variables = data.get("variables", {})

    if not isinstance(project, str) or not isinstance(variables, dict):
        _err("Invalid vars.yml structure (missing project/variables).")

    if project.strip() != spec.project.strip():
        _err(
            "Project mismatch:\n"
            f"  deploy.yml project: {spec.project}\n"
            f"  vars.yml   project: {project}\n"
            "Refusing to plan to avoid mistakes."
        )

    # Build env map deterministically with env_scope filtering
    env: Dict[str, str] = {}
    included_keys: list[str] = []

    for key, meta in variables.items():
        if not isinstance(key, str) or not isinstance(meta, dict):
            continue

        var_scope = str(meta.get("environment_scope", "*") or "*").strip() or "*"

        # If target_scope is "*": include everything (legacy behavior)
        # Else: include globals ("*") + only matching scope
        if target_scope != "*" and not (var_scope == "*" or var_scope == target_scope):
            continue

        val = meta.get("value", "")
        env[key] = "" if val is None else str(val)
        included_keys.append(key)

    # Deterministic hints
    env.setdefault("DLANE_TARGET", resolved_target)
    env.setdefault("DLANE_DEPLOY_DIR", t.deploy_dir)
    env.setdefault("DLANE_HOST", t.host)
    env.setdefault("DLANE_USER", t.user)
    env.setdefault("DEPLOY_STRATEGY", strategy)
    if getattr(t, "health_host", ""):
        env.setdefault("HEALTH_HOST", t.health_host)

    # Where render would write
    out_path = out_dir / resolved_target / ".env"

    # Validate required keys (soft validation: plan shows missing)
    missing: list[str] = []

    # Common expectations for server-side deploy scripts
    common_required = ["REGISTRY_USER", "REGISTRY_PASS"]
    for k in common_required:
        if k not in env or not str(env.get(k, "")).strip():
            missing.append(k)

    # Strategy-specific expectations
    if strategy == "bluegreen":
        # These are typical keys in your bg script; adjust names if you standardize differently
        bg_required = ["ACTIVE_COLOR", "NEXT_TAG_BLUE", "NEXT_TAG_GREEN", "REGISTRY_HOST"]
        for k in bg_required:
            if k not in env or not str(env.get(k, "")).strip():
                missing.append(k)
    else:
        # plain strategy: we don't switch nginx; typically one service + one tag key + one port
        plain_required = ["PLAIN_SERVICE", "PLAIN_TAG_KEY", "PLAIN_PORT", "REGISTRY_HOST"]
        for k in plain_required:
            if k not in env or not str(env.get(k, "")).strip():
                missing.append(k)

    # Print plan deterministically
    typer.secho("DEPLOY PLAN", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Project      : {spec.project}")
    typer.echo(f"Target       : {resolved_target}")
    typer.echo(f"Target scope : {target_scope}")
    typer.echo(f"Strategy     : {strategy}")
    typer.echo("")
    typer.echo(f"Host         : {t.host}")
    typer.echo(f"User         : {t.user}")
    typer.echo(f"Deploy dir   : {t.deploy_dir}")
    typer.echo(f"Health host  : {getattr(t, 'health_host', '') or '-'}")
    typer.echo("")
    typer.echo(f"Vars file    : {vars_file}")
    typer.echo(f"Deploy file  : {deploy_file}")
    typer.echo(f"Render output: {out_path}")
    typer.echo(f"Script (srv) : {script}")
    typer.echo("")

    included_keys = sorted(set(included_keys), key=lambda s: s.lower())
    typer.secho(f"Included vars ({len(included_keys)})", fg=typer.colors.GREEN, bold=True)
    typer.echo("  " + ", ".join(included_keys) if included_keys else "  -")
    typer.echo("")

    if missing:
        missing = sorted(set(missing), key=lambda s: s.lower())
        typer.secho("Missing required keys for this strategy:", fg=typer.colors.RED, bold=True)
        for k in missing:
            typer.echo(f"  - {k}")
        typer.echo("")
        typer.secho("Tip: add them into vars.yml (with correct environment_scope) then re-render.", fg=typer.colors.YELLOW)

    # Show the future deterministic execution commands (no execution now)
    typer.secho("Planned actions (no execution):", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"  1) dlane deploy render --target {resolved_target}")
    typer.echo(f"  2) (on server) {script} prepare {t.deploy_dir} <sha-tag> $REGISTRY_USER $REGISTRY_PASS")
    if strategy == "bluegreen":
        typer.echo(f"  3) (on server) {script} auto-switch {t.deploy_dir}")
    else:
        typer.echo("  3) (plain) no traffic switch step")

@deploy_app.command("proof")
def deploy_proof(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Target name (defaults: deploy.yml default_target or single target)"
    ),
    deploy_file: Path = typer.Option(DEPLOY_YML_DEFAULT, "--deploy-file", help="Deploy spec YAML path"),
    vars_file: Path = typer.Option(VARS_YML_DEFAULT, "--vars-file", help="Vars YAML path"),
    out_dir: Path = typer.Option(RENDER_DIR_DEFAULT, "--out-dir", help="Rendered output base directory"),
    extra: List[Path] = typer.Option(
        [], "--extra",
        help="Extra local file to include in proof (repeatable)",
    ),
):
    """
    Create a deterministic proof manifest for a target.
    Output:
      .deploylane/rendered/<target>/proof.json
    """
    if not deploy_file.exists():
        _err(f"deploy.yml not found: {deploy_file}")

    if not vars_file.exists():
        _err(f"vars.yml not found: {vars_file}")

    try:
        spec = load_deployspec(deploy_file)
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    # Resolve target deterministically
    resolved_target = target
    if not resolved_target:
        if getattr(spec, "default_target", None):
            resolved_target = spec.default_target
        elif len(spec.targets) == 1:
            resolved_target = next(iter(spec.targets.keys()))

    if not resolved_target:
        typer.echo("Missing --target. Available targets:")
        for name in sorted(spec.targets.keys()):
            typer.echo(f"  - {name}")
        typer.echo("")
        typer.echo("Tip: set 'default_target' in .deploylane/deploy.yml or pass --target <name>.")
        raise typer.Exit(code=1)

    if resolved_target not in spec.targets:
        available = ", ".join(sorted(spec.targets.keys()))
        _err(f"Unknown target '{resolved_target}'. Available: {available}")

    t = spec.targets[resolved_target]

    # Expect render already done (proof locks the rendered env)
    env_path = out_dir / resolved_target / ".env"
    if not env_path.exists():
        _err(f"rendered .env not found: {env_path} (run: dlane deploy render)")

    # Optional conventional files (only if exist locally)
    optional_paths: List[Path] = []
    p1 = Path(".deploylane/deploy.sh")
    if p1.exists():
        optional_paths.append(p1)

    p2 = Path("docker-compose.yml")
    if p2.exists():
        optional_paths.append(p2)

    for x in extra:
        if x.exists():
            optional_paths.append(x)
        else:
            typer.secho(f"skip (missing): {x}", fg=typer.colors.YELLOW)

    # Deterministic file list
    base_files = [deploy_file, vars_file, env_path]
    all_files = base_files + optional_paths

    # Use cwd as root for nicer paths (still deterministic)
    root = Path.cwd()

    files = [file_entry(p, root=root) for p in all_files]
    files = sorted(files, key=lambda d: str(d.get("path", "")))

    proof = {
        "tool": "deploylane",
        "kind": "deploy-proof",
        "project": spec.project,
        "target": resolved_target,
        "strategy": getattr(t, "strategy", "plain"),
        "deploy_dir": getattr(t, "deploy_dir", ""),
        "host": getattr(t, "host", ""),
        "user": getattr(t, "user", ""),
        "files": files,
    }

    proof_path = out_dir / resolved_target / "proof.json"
    write_json_deterministic(proof_path, proof)

    typer.secho("Proof OK", fg=typer.colors.GREEN)
    typer.echo(f"Project : {spec.project}")
    typer.echo(f"Target  : {resolved_target}")
    typer.echo(f"Output  : {proof_path}")
    typer.echo(f"Files   : {len(files)}")

@deploy_app.command("push")
def deploy_push(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Target name (defaults: deploy.yml default_target or single target)"
    ),
    deploy_file: Path = typer.Option(DEPLOY_YML_DEFAULT, "--deploy-file", help="Deploy spec YAML path"),
    vars_file: Path = typer.Option(VARS_YML_DEFAULT, "--vars-file", help="Vars YAML path"),
    out_dir: Path = typer.Option(RENDER_DIR_DEFAULT, "--out-dir", help="Rendered output base directory"),
    remote_base: str = typer.Option(
        ".deploylane/packets",
        "--remote-base",
        help="Remote base directory under deploy_dir to store pushed packets",
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually push (otherwise dry-run)"),
):
    """
    Push rendered artifacts to the target server.
    - Uses deploy.yml host/user/deploy_dir
    - Copies: .deploylane/rendered/<target>/ (typically .env + proof.json)
    - Does NOT execute anything on the server
    """
    if not deploy_file.exists():
        _err(f"deploy.yml not found: {deploy_file}")
    if not vars_file.exists():
        _err(f"vars.yml not found: {vars_file} (run: dlane vars get --project <path>)")

    try:
        spec = load_deployspec(deploy_file)
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    # Resolve target deterministically (same as others)
    resolved_target = target
    if not resolved_target:
        if getattr(spec, "default_target", None):
            resolved_target = spec.default_target
        elif len(spec.targets) == 1:
            resolved_target = next(iter(spec.targets.keys()))

    if not resolved_target:
        typer.echo("Missing --target. Available targets:")
        for name in sorted(spec.targets.keys()):
            typer.echo(f"  - {name}")
        raise typer.Exit(code=1)

    if resolved_target not in spec.targets:
        available = ", ".join(sorted(spec.targets.keys()))
        _err(f"Unknown target '{resolved_target}'. Available: {available}")

    t = spec.targets[resolved_target]

    # Ensure render exists; if not, render now (local only)
    rendered_dir = out_dir / resolved_target
    env_path = rendered_dir / ".env"
    if not env_path.exists():
        typer.secho("Rendered .env not found → rendering now...", fg=typer.colors.YELLOW)
        # Minimal render (same behavior as deploy render)
        data = read_vars_file(vars_file)
        project = data.get("project")
        variables = data.get("variables", {})
        if not isinstance(project, str) or not isinstance(variables, dict):
            _err("Invalid vars.yml structure (missing project/variables).")
        if project.strip() != spec.project.strip():
            _err(
                "Project mismatch:\n"
                f"  deploy.yml project: {spec.project}\n"
                f"  vars.yml   project: {project}\n"
                "Refusing to render to avoid mistakes."
            )

        env: Dict[str, str] = {}
        for key, meta in variables.items():
            if not isinstance(key, str) or not isinstance(meta, dict):
                continue
            val = meta.get("value", "")
            env[key] = "" if val is None else str(val)

        env.setdefault("DLANE_TARGET", resolved_target)
        write_env_file(env_path, env)

    # Ensure proof exists; if not, create now (locks current rendered env)
    proof_path = rendered_dir / "proof.json"
    if not proof_path.exists():
        typer.secho("proof.json not found → generating now...", fg=typer.colors.YELLOW)

        root = Path.cwd()
        files = []
        # Keep proof minimal for push: deploy.yml + rendered .env + (vars.yml is hashed but NOT shipped by default)
        for p in [deploy_file, vars_file, env_path]:
            files.append(file_entry(p, root=root))
        files = sorted(files, key=lambda d: str(d.get("path", "")))

        proof = {
            "tool": "deploylane",
            "kind": "deploy-proof",
            "project": spec.project,
            "target": resolved_target,
            "strategy": getattr(t, "strategy", "plain"),
            "deploy_dir": getattr(t, "deploy_dir", ""),
            "host": getattr(t, "host", ""),
            "user": getattr(t, "user", ""),
            "files": files,
        }
        write_json_deterministic(proof_path, proof)

    # Packet id: stable-ish + readable
    # Use proof.json sha (ties push to exact artifact set)
    proof_sha = sha256_file(proof_path)[:12]
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    packet_id = f"{stamp}-{proof_sha}"

    # Remote path
    deploy_dir_remote = str(getattr(t, "deploy_dir", "")).strip()
    if not deploy_dir_remote:
        _err("deploy.yml target missing deploy_dir")

    remote_dir = f"{deploy_dir_remote.rstrip('/')}/{remote_base.strip().strip('/')}/{resolved_target}/{packet_id}"

    typer.secho("DEPLOY PUSH", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Target     : {resolved_target}")
    typer.echo(f"Host       : {t.host}")
    typer.echo(f"User       : {t.user}")
    typer.echo(f"Deploy dir : {t.deploy_dir}")
    typer.echo(f"Local dir  : {rendered_dir}")
    typer.echo(f"Remote dir : {remote_dir}")
    typer.echo(f"Mode       : {'REAL' if yes else 'DRY-RUN'}")
    typer.echo("")

    try:
        method = copy_dir(rendered_dir, f"{t.user}@{t.host}", remote_dir, dry_run=(not yes))
    except RemoteError as e:
        _err(str(e))
        
    # Update "latest" symlink for convenience (only when real push)
    latest_link = f"{deploy_dir_remote.rstrip('/')}/{remote_base.strip().strip('/')}/{resolved_target}/latest"
    if yes:
        try:
            from .remote import ssh_symlink
            ssh_symlink(f"{t.user}@{t.host}", remote_dir, latest_link, dry_run=False)
            typer.echo(f"Latest link : {latest_link} -> {remote_dir}")
        except Exception:
            # don't fail the push for symlink issues
            typer.secho("Note: could not update latest symlink.", fg=typer.colors.YELLOW)

    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo(f"Method     : {method}")
    typer.echo(f"Pushed     : {rendered_dir} -> {t.user}@{t.host}:{remote_dir}")
    typer.echo("")
    typer.secho("Next (manual):", fg=typer.colors.YELLOW)
    typer.echo(f"  ssh {t.user}@{t.host} 'ls -la {remote_dir}'")

@deploy_app.command("run")
def deploy_run(
    ctx: typer.Context,
    target: Optional[str] = typer.Option(
        None, "--target",
        help="Target name (defaults: deploy.yml default_target or single target)"
    ),
    deploy_file: Path = typer.Option(DEPLOY_YML_DEFAULT, "--deploy-file", help="Deploy spec YAML path"),
    out_dir: Path = typer.Option(RENDER_DIR_DEFAULT, "--out-dir", help="Rendered output base directory"),
    remote_base: str = typer.Option(
        ".deploylane/packets",
        "--remote-base",
        help="Remote base directory under deploy_dir where packets were pushed",
    ),
    packet_id: Optional[str] = typer.Option(
        None,
        "--packet-id",
        help="Packet id to run (default: latest)",
    ),
    script: str = typer.Option(
        ".deploylane/scripts/deploy.sh",
        "--script",
        help="Server-side deploy script path",
    ),
    image_tag: Optional[str] = typer.Option(
        None,
        "--image-tag",
        help="Image tag to deploy (e.g. commit sha, build tag). REQUIRED.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Actually execute (otherwise dry-run)"),
):
    """
    Execute server-side deployment script using previously pushed packet.
    - Sources remote .env from packet directory
    - Runs: <script> prepare ...
    - If strategy=bluegreen also runs: <script> auto-switch ...
    Default uses 'latest' packet symlink unless --packet-id is provided.
    """
    if not deploy_file.exists():
        _err(f"deploy.yml not found: {deploy_file}")

    if not image_tag:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=1)

    try:
        spec = load_deployspec(deploy_file)
    except Exception as e:
        _err(f"Invalid deploy.yml: {e}")

    # Resolve target deterministically
    resolved_target = target
    if not resolved_target:
        if getattr(spec, "default_target", None):
            resolved_target = spec.default_target
        elif len(spec.targets) == 1:
            resolved_target = next(iter(spec.targets.keys()))

    if not resolved_target:
        typer.echo("Missing --target. Available targets:")
        for name in sorted(spec.targets.keys()):
            typer.echo(f"  - {name}")
        raise typer.Exit(code=1)

    if resolved_target not in spec.targets:
        available = ", ".join(sorted(spec.targets.keys()))
        _err(f"Unknown target '{resolved_target}'. Available: {available}")

    t = spec.targets[resolved_target]
    strategy = (getattr(t, "strategy", "plain") or "plain").strip() or "plain"

    deploy_dir_remote = str(getattr(t, "deploy_dir", "")).strip()
    if not deploy_dir_remote:
        _err("deploy.yml target missing deploy_dir")

    # Remote packet dir (latest or specific id)
    pid = (packet_id or "latest").strip()
    remote_dir = f"{deploy_dir_remote.rstrip('/')}/{remote_base.strip().strip('/')}/{resolved_target}/{pid}"

    # Remote env path
    remote_env = f"{remote_dir.rstrip('/')}/.env"

    dest = f"{t.user}@{t.host}"

    typer.secho("DEPLOY RUN", fg=typer.colors.CYAN, bold=True)
    typer.echo(f"Target     : {resolved_target}")
    typer.echo(f"Host       : {t.host}")
    typer.echo(f"User       : {t.user}")
    typer.echo(f"Deploy dir : {t.deploy_dir}")
    typer.echo(f"Strategy   : {strategy}")
    typer.echo(f"Packet     : {pid}")
    typer.echo(f"Remote dir : {remote_dir}")
    typer.echo(f"Env file   : {remote_env}")
    typer.echo(f"Script     : {script}")
    typer.echo(f"Image tag  : {image_tag}")
    typer.echo(f"Mode       : {'REAL' if yes else 'DRY-RUN'}")
    typer.echo("")

    # Build remote command:
    # - source .env (export everything)
    # - run script prepare
    # - optional bluegreen switch
    #
    # We avoid leaking secrets in output by NOT echoing env contents.
    base = (
        "set -e; "
        f"test -f {remote_env} || (echo 'missing .env: {remote_env}' >&2; exit 2); "
        "set -a; "
        f". {remote_env}; "
        "set +a; "
    )

    # prepare step expects registry creds in env:
    # script prepare <deploy_dir> <image_tag> "$REGISTRY_USER" "$REGISTRY_PASS"
    prepare_cmd = (
        f"{script} prepare {deploy_dir_remote} {image_tag} "
        "\"${REGISTRY_USER:-}\" \"${REGISTRY_PASS:-}\""
    )

    if strategy == "bluegreen":
        switch_cmd = f"{script} auto-switch {deploy_dir_remote}"
        remote_cmd = base + prepare_cmd + "; " + switch_cmd
    else:
        remote_cmd = base + prepare_cmd

    try:
        ssh_run(dest, remote_cmd, dry_run=(not yes))
    except (RemoteError, Exception) as e:
        _err(str(e))

    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo("")
    typer.secho("Tips:", fg=typer.colors.YELLOW)
    typer.echo(f"  - To run a specific packet: dlane deploy run --target {resolved_target} --packet-id <id> --image-tag {image_tag} --yes")
    typer.echo(f"  - To inspect remote packet: ssh {dest} 'ls -la {remote_dir}'")

if __name__ == "__main__":
    app()