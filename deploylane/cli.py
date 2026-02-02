from __future__ import annotations
from typing import Dict, Optional, Tuple, Any, List
import typer
import sys
from typing import Optional
from typer.main import get_command
from pathlib import Path
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
    set_project_variable
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

app = typer.Typer(add_completion=True, no_args_is_help=True)


SKIP_PROFILE_BANNER = {
    "profile-use",
    "profiles-list",
    "login", 
    "logout",
}

@app.callback(invoke_without_command=False)
def _global_callback(ctx: typer.Context) -> None:
    # Don't print during help/completion parsing
    if getattr(ctx, "resilient_parsing", False):
        return

    # IMPORTANT: actual command name
    sub = (ctx.invoked_subcommand or "").strip()

    if sub in SKIP_PROFILE_BANNER:
        return

    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)

    typer.secho(f"▶ profile: {active}", fg=typer.colors.GREEN, bold=True)
    if prof is None:
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
        prof, user = do_login(profile_name=profile, host=host, token=token)
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
    typer.echo(f"has_profile    : {s.get('has_profile')}")
    typer.echo(f"host           : {s.get('host') or '-'}")
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


@app.command()
def config_show():
    """Print config path and active profile (debug helper)."""
    cfg = load_config()
    typer.echo(f"path   : {config_path()}")
    typer.echo(f"active : {get_active_profile_name(cfg)}")


@app.command()
def host_normalize(host: str):
    """Debug helper: show the normalized host."""
    typer.echo(normalize_host(host))



@app.command("projects-list")
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
        
@app.command("vars-get")
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

        data = {
            "project": project,
            "scope": "*",
            "variables": {},
        }

        for v in vars_list:
            data["variables"][v.key] = {
                "value": v.value or "",
                "masked": v.masked,
                "protected": v.protected,
                "environment_scope": v.environment_scope,
            }

    write_vars_file(out, data)

    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo(f"Saved → {out}")

@app.command("vars-apply")
def vars_set(
    ctx: typer.Context,
    file: Path = typer.Option(DEFAULT_FILE, "--file", help="YAML file to apply"),
):
    """
    Apply variables from a YAML file into GitLab project variables.
    """
    prof = get_active_profile_or_exit()

    if not file.exists():
        _err(f"YAML file not found: {file}")

    data = read_vars_file(file)

    project = data.get("project")
    scope = data.get("scope", "*")
    variables = data.get("variables", {})

    if not project or not isinstance(variables, dict):
        _err("Invalid YAML structure (missing project/variables).")

    try:
        prj = get_project_by_path(prof.host, prof.token, project)
    except GitLabError as e:
        _err(str(e))

    typer.echo(f"Applying {len(variables)} variables to {project}...")

    for key, meta in variables.items():
        value = str(meta.get("value", ""))
        masked = bool(meta.get("masked", False))
        protected = bool(meta.get("protected", False))
        env_scope = meta.get("environment_scope", scope)

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
            )
            typer.echo(f"  OK {key}")
        except GitLabError as e:
            typer.echo(f"  FAIL {key}: {e}")

    typer.secho("DONE", fg=typer.colors.GREEN)
    


@app.command("vars-diff")
def vars_diff(
    project: str = typer.Option(..., "--project", help="Project path_with_namespace (e.g. sachane/sachane-next)"),
    file: Optional[Path] = typer.Option(None, "--file", help="YAML file path (default: .deploylane/vars.yml)"),
    scope: str = typer.Option("*", "--scope", help="Environment scope to compare (default: *)"),
    show_values: bool = typer.Option(False, "--show-values", help="Print values in diff (careful: secrets)"),
    exit_code: bool = typer.Option(True, "--exit-code/--no-exit-code", help="Exit with code 2 if changes exist"),
):
    """
    Show diff between GitLab variables and local vars.yml (deterministic ordering).
    """
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if not prof:
        _err("Not logged in. Run: dlane login")

    if file is None:
        file = Path.cwd() / ".deploylane" / "vars.yml"

    # 1) Load local YAML (the same structure produced by vars-get)
    try:
        local_doc = load_vars_yml(file)  # <-- your existing YAML loader
    except FileNotFoundError:
        _err(f"vars file not found: {file}")
    except Exception as e:
        _err(f"failed to read vars yaml: {e}")

    local_project = str(local_doc.get("project") or "")
    local_scope = str(local_doc.get("scope") or "*")
    if local_project and local_project != project:
        _err(f"vars.yml project mismatch: file has '{local_project}', you passed '{project}'")
    if local_scope and local_scope != scope:
        # not fatal, but deterministic: we compare requested scope
        pass

    local_vars_raw = local_doc.get("variables") or {}
    if not isinstance(local_vars_raw, dict):
        _err("Invalid vars.yml: 'variables' must be a mapping")

    local_vars: Dict[str, VarSpec] = {}
    for k, v in local_vars_raw.items():
        if not isinstance(v, dict):
            continue
        spec = _norm_var(v)
        # compare only selected scope (or '*' by default)
        if spec.environment_scope != scope:
            continue
        local_vars[str(k)] = spec

    # 2) Load remote variables from GitLab (you already have vars-list logic)
    try:
        prj = get_project_by_path(prof.host, prof.token, project)
        remote_items = list_project_variables(  # <-- your existing GitLab call
            prof.host,
            prof.token,
            prj.id
        )
    except Exception as e:
        _err(str(e))

    remote_vars: Dict[str, VarSpec] = {}
    for it in remote_items:
        # adapt if your remote object is dict or dataclass
        if isinstance(it, dict):
            key = str(it.get("key", ""))
            if not key:
                continue
            remote_vars[key] = VarSpec(
                value=str(it.get("value", "")),
                masked=bool(it.get("masked", False)),
                protected=bool(it.get("protected", False)),
                environment_scope=str(it.get("environment_scope", "*") or "*"),
            )
        else:
            # dataclass style
            key = getattr(it, "key", "")
            if not key:
                continue
            remote_vars[str(key)] = VarSpec(
                value=str(getattr(it, "value", "")),
                masked=bool(getattr(it, "masked", False)),
                protected=bool(getattr(it, "protected", False)),
                environment_scope=str(getattr(it, "environment_scope", "*") or "*"),
            )

    # 3) Diff (deterministic)
    local_keys = set(local_vars.keys())
    remote_keys = set(remote_vars.keys())

    added = sorted(local_keys - remote_keys)
    removed = sorted(remote_keys - local_keys)
    common = sorted(local_keys & remote_keys)

    changed: List[Tuple[str, VarSpec, VarSpec]] = []
    for k in common:
        a = remote_vars[k]
        b = local_vars[k]
        if a != b:
            changed.append((k, a, b))

    has_changes = bool(added or removed or changed)

    # 4) Print
    typer.echo(f"Project: {project}")
    typer.echo(f"Scope  : {scope}")
    typer.echo(f"File   : {file}")
    typer.echo("")

    if not has_changes:
        typer.secho("No changes.", fg=typer.colors.GREEN)
        return

    if added:
        typer.secho(f"+ Added ({len(added)}):", fg=typer.colors.GREEN)
        for k in added:
            spec = local_vars[k]
            val = _safe_value(spec, show_values)
            typer.echo(f"  + {k} = {val}   masked={spec.masked} protected={spec.protected} env={spec.environment_scope}")
        typer.echo("")

    if removed:
        typer.secho(f"- Removed ({len(removed)}):", fg=typer.colors.RED)
        for k in removed:
            spec = remote_vars[k]
            val = _safe_value(spec, show_values)
            typer.echo(f"  - {k} = {val}   masked={spec.masked} protected={spec.protected} env={spec.environment_scope}")
        typer.echo("")

    if changed:
        typer.secho(f"~ Changed ({len(changed)}):", fg=typer.colors.YELLOW)
        for k, old, new in sorted(changed, key=lambda x: x[0]):
            oldv = _safe_value(old, show_values)
            newv = _safe_value(new, show_values)
            typer.echo(f"  ~ {k}")
            typer.echo(f"    - value: {oldv}")
            typer.echo(f"    + value: {newv}")
            if old.masked != new.masked:
                typer.echo(f"    - masked: {old.masked}")
                typer.echo(f"    + masked: {new.masked}")
            if old.protected != new.protected:
                typer.echo(f"    - protected: {old.protected}")
                typer.echo(f"    + protected: {new.protected}")
            if old.environment_scope != new.environment_scope:
                typer.echo(f"    - env: {old.environment_scope}")
                typer.echo(f"    + env: {new.environment_scope}")
        typer.echo("")

    if exit_code:
        raise typer.Exit(code=2)

@app.command("profiles-list")
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

@app.command("profile-use")
def profile_use(
    profile: str = typer.Option(..., "--profile", help="Profile name to activate"),
) -> None:
    """Set active profile (does not modify credentials)."""
    cfg = load_config()

    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict) or profile not in profiles:
        _err(f"Profile not found: {profile}. Use: dlane profiles-list")

    set_active_profile_name(cfg, profile)
    save_config(cfg)

    typer.secho("OK", fg=typer.colors.GREEN)
    typer.echo(f"Active profile set to: {profile}")
    typer.echo(f"Config: {config_path()}")

if __name__ == "__main__":
    app()