from __future__ import annotations

from typing import Optional

import typer

from ..auth import (
    login as do_login,
    logout as do_logout,
    load_active_profile,
    status as do_status,
    get_provider,
    AuthError,
)
from ..config import (
    DEFAULT_HOST,
    config_path,
    load_config,
    get_active_profile_name,
    get_profile,
    set_active_profile_name,
    save_config,
    env_fallback_host,
    env_fallback_token,
    env_fallback_platform,
    normalize_host,
)
from ..providers.base import ProviderError
from ._utils import _err

# ─── Sub-apps ─────────────────────────────────────────────────────────────────

config_app = typer.Typer(no_args_is_help=True, help="Config helpers (debug).")
profile_app = typer.Typer(no_args_is_help=True, help="Manage local profiles.")


# ─── UX helpers ───────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    typer.secho(f"  ✓  {msg}", fg=typer.colors.GREEN)


def _warn(msg: str) -> None:
    typer.secho(f"  ⚠  {msg}", fg=typer.colors.YELLOW)


def _hint(msg: str) -> None:
    typer.secho(f"     {msg}", fg=typer.colors.CYAN)


def _token_url(host: str, platform: str) -> str:
    if platform == "github":
        return f"{host}/settings/tokens"
    return f"{host}/-/user_settings/personal_access_tokens"


# ─── Top-level commands (registered on main app by cli.py) ───────────────────

def login(
    host: Optional[str] = typer.Option(None, "--host", help="Host URL (e.g. https://gitlab.com or https://github.com)"),
    token: Optional[str] = typer.Option(None, "--token", help="Personal Access Token"),
    platform: Optional[str] = typer.Option(None, "--platform", help="Platform: gitlab or github (default: gitlab)"),
    profile: str = typer.Option("default", "--profile", help="Profile name (stored locally)"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail instead of prompting"),
    registry_host: Optional[str] = typer.Option(
        None, "--registry-host", help="Docker registry host (optional, GitLab only)"
    ),
):
    """Store credentials locally and verify against the API."""
    if host is None:
        host = env_fallback_host()
    if token is None:
        token = env_fallback_token()
    if platform is None:
        platform = env_fallback_platform() or "gitlab"

    if platform not in ("gitlab", "github"):
        _err(f"Unknown platform '{platform}'. Supported: gitlab, github")

    if non_interactive:
        if not host or not token:
            _err(
                "Missing --host/--token in non-interactive mode.\n"
                "  GitLab: set GITLAB_HOST and GITLAB_TOKEN\n"
                "  GitHub: set GITHUB_HOST (optional) and GITHUB_TOKEN"
            )
    else:
        if not host:
            default_host = "https://gitlab.com" if platform == "gitlab" else "https://github.com"
            host = typer.prompt(f"\n  {platform.title()} host", default=default_host)

        if not token:
            resolved_host = normalize_host(host)
            typer.echo("")
            _hint(f"Get a token at: {_token_url(resolved_host, platform)}")
            _hint("Required scope: api")
            typer.echo("")
            token = typer.prompt(f"  {platform.title()} token (PAT)", hide_input=True)

    assert host is not None and token is not None

    resolved_host = normalize_host(host)
    typer.echo("")
    typer.secho(f"  Connecting to {resolved_host}...", fg=typer.colors.CYAN)

    try:
        prof, user = do_login(
            profile_name=profile, host=host, token=token,
            registry_host=(registry_host or ""), platform=platform,
        )
    except (AuthError, ProviderError) as e:
        typer.echo("")
        _err(str(e))

    username = getattr(user, "username", "-")
    name = getattr(user, "name", "")
    display = f"{username} ({name})" if name and name != username else username

    typer.echo("")
    _ok(f"Logged in as {display}")
    typer.echo("")
    typer.echo(f"  Profile  : {prof.name}")
    typer.echo(f"  Host     : {prof.host}")
    typer.echo(f"  Platform : {prof.platform}")
    typer.echo(f"  Config   : {config_path()}")

    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if isinstance(profiles, dict) and len(profiles) == 1:
        typer.echo("")
        _hint("Tip: use --profile <name> to store multiple accounts")
        _hint("     e.g. dlane login --profile staging --host https://staging.example.com")
    typer.echo("")


def whoami():
    """Show current user for the active profile (requires login)."""
    try:
        prof = load_active_profile()
        u = get_provider(prof).whoami()
    except (AuthError, ProviderError) as e:
        _err(str(e))

    username = u.username
    name = getattr(u, "name", "")
    display = f"{username} ({name})" if name and name != username else username

    typer.echo("")
    typer.secho(f"  {display}", bold=True)
    typer.echo(f"  Host     : {prof.host}")
    typer.echo(f"  Platform : {prof.platform}")
    typer.echo(f"  Profile  : {prof.name}")
    typer.echo("")


def logout(
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile to remove (default: active)"),
    all_profiles: bool = typer.Option(False, "--all", help="Remove all stored profiles"),
    yes: bool = typer.Option(False, "-y", "--yes", help="Do not prompt"),
):
    """Remove stored credentials from the local config."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    target = profile or active

    # Capture profile info before deletion so we can offer re-login
    existing_prof = get_profile(cfg, target)

    if all_profiles:
        profiles = cfg.get("profiles", {})
        count = len(profiles) if isinstance(profiles, dict) else 0
        if not yes and not typer.confirm(f"\n  Remove all {count} profile(s) from {config_path()}?"):
            raise typer.Exit(code=0)
        do_logout(profile_name=target, all_profiles=True)
        typer.echo("")
        _ok(f"Removed {count} profile(s)")
        _hint("Run 'dlane login' to add a new account")
        typer.echo("")
        return

    if not yes and not typer.confirm(f"\n  Remove profile '{target}' from {config_path()}?"):
        raise typer.Exit(code=0)

    removed = do_logout(profile_name=target, all_profiles=False)
    typer.echo("")
    if not removed:
        _warn(f"No such profile: {target}")
        typer.echo("")
        return

    _ok(f"Logged out: {target}")

    # In interactive mode, offer to re-login immediately (only token needed)
    if not yes and existing_prof:
        typer.echo("")
        if typer.confirm(f"  Log in to '{target}' again?", default=False):
            typer.echo("")
            _hint(f"Host     : {existing_prof.host}  (unchanged)")
            _hint(f"Platform : {existing_prof.platform}")
            _hint(f"Token URL: {_token_url(existing_prof.host, existing_prof.platform)}")
            typer.echo("")
            new_token = typer.prompt(f"  New {existing_prof.platform.title()} token (PAT)", hide_input=True)
            typer.echo("")
            typer.secho(f"  Connecting to {existing_prof.host}...", fg=typer.colors.CYAN)
            try:
                prof, user = do_login(
                    profile_name=target,
                    host=existing_prof.host,
                    token=new_token,
                    registry_host=existing_prof.registry_host,
                    platform=existing_prof.platform,
                )
                username = getattr(user, "username", "-")
                name = getattr(user, "name", "")
                display = f"{username} ({name})" if name and name != username else username
                typer.echo("")
                _ok(f"Logged in as {display}")
                typer.echo(f"     Profile  : {prof.name}")
                typer.echo(f"     Host     : {prof.host}")
            except (AuthError, ProviderError) as e:
                typer.echo("")
                _warn(f"Login failed: {e}")
                _hint(f"Run: dlane login --profile {target} --host {existing_prof.host}")
            typer.echo("")
            return

    # Show what to do next
    cfg2 = load_config()
    remaining = cfg2.get("profiles", {})
    if isinstance(remaining, dict) and remaining:
        names = sorted(remaining.keys())
        _hint(f"Other profiles: {', '.join(names)}")
        _hint("Run 'dlane profile use' to switch")
    else:
        _hint(f"Run: dlane login --profile {target}")
    typer.echo("")


# ─── config_app commands ──────────────────────────────────────────────────────

@config_app.command("show")
def config_show():
    """Print config path and active profile (debug helper)."""
    cfg = load_config()
    typer.echo(f"path   : {config_path()}")
    typer.echo(f"active : {get_active_profile_name(cfg)}")


# ─── profile_app commands ─────────────────────────────────────────────────────

@profile_app.command("list")
def profiles_list() -> None:
    """List stored profiles and show the active one."""
    cfg = load_config()
    active = get_active_profile_name(cfg)

    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        typer.echo("")
        _warn("No profiles found.")
        _hint("Run: dlane login")
        typer.echo("")
        raise typer.Exit(code=1)

    names = sorted(profiles.keys())
    w_name = max(len(n) for n in names)
    w_host = max((len(profiles[n].get("host", "")) for n in names), default=4)

    typer.echo("")
    typer.secho("  Profiles:", bold=True)
    typer.echo("")

    for n in names:
        p = profiles.get(n, {})
        host = p.get("host", "")
        platform = p.get("platform", "gitlab")
        is_active = n == active

        mark = typer.style("✓", fg=typer.colors.GREEN) if is_active else " "
        name_col = typer.style(f"{n:<{w_name}}", fg=typer.colors.GREEN, bold=True) if is_active else f"{n:<{w_name}}"
        host_col = f"{host:<{w_host}}"
        platform_col = typer.style(f"[{platform}]", fg=typer.colors.CYAN)
        active_tag = typer.style("  ← active", fg=typer.colors.GREEN) if is_active else ""

        typer.echo(f"  {mark}  {name_col}  {host_col}  {platform_col}{active_tag}")

    typer.echo("")
    typer.secho(f"  Active: {active}", fg=typer.colors.GREEN)
    typer.echo("")
    _hint("Run 'dlane profile use <name>' to switch")
    typer.echo("")


@profile_app.command("use")
def profile_use(
    profile: Optional[str] = typer.Argument(None, help="Profile name to activate", show_default=False),
):
    """Switch the active profile."""
    cfg = load_config()

    if not profile:
        profiles = cfg.get("profiles", {})
        names = sorted(profiles.keys()) if isinstance(profiles, dict) else []

        if not names:
            typer.echo("")
            _warn("No profiles found.")
            _hint("Run: dlane login --profile <name>")
            typer.echo("")
            raise typer.Exit(1)

        active = get_active_profile_name(cfg)
        w_name = max(len(n) for n in names)

        typer.echo("")
        typer.secho("  Select a profile:", bold=True)
        typer.echo("")

        for i, n in enumerate(names, 1):
            p = profiles.get(n, {})
            host = p.get("host", "")
            platform = p.get("platform", "gitlab")
            is_active = n == active

            mark = typer.style("✓", fg=typer.colors.GREEN) if is_active else " "
            num = typer.style(f"{i}.", bold=True)
            name_col = typer.style(f"{n:<{w_name}}", fg=typer.colors.GREEN) if is_active else f"{n:<{w_name}}"
            active_tag = typer.style("  (active)", fg=typer.colors.GREEN) if is_active else ""
            typer.echo(f"  {mark}  {num}  {name_col}  {host}  [{platform}]{active_tag}")

        typer.echo("")
        default_num = str(names.index(active) + 1) if active in names else "1"
        raw = typer.prompt(f"  Profile name or number", default=default_num).strip()
        typer.echo("")

        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(names):
                profile = names[idx]
            else:
                _warn(f"Invalid number: {raw}. Enter 1–{len(names)}.")
                raise typer.Exit(1)
        else:
            profile = raw

    prof = get_profile(cfg, profile)
    if not prof:
        typer.echo("")
        _warn(f"Profile not found: {profile}")
        typer.echo("")
        raise typer.Exit(1)

    set_active_profile_name(cfg, profile)
    save_config(cfg)

    typer.echo("")
    _ok(f"Switched to '{profile}'")
    typer.echo(f"     Host     : {prof.host}")
    typer.echo(f"     Platform : {prof.platform}")
    typer.echo("")
