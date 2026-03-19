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
    normalize_host,
)
from ..providers.base import ProviderError
from ._utils import _err

# ─── Sub-apps ─────────────────────────────────────────────────────────────────

config_app = typer.Typer(no_args_is_help=True, help="Config helpers (debug).")
profile_app = typer.Typer(no_args_is_help=True, help="Manage local profiles.")


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
    from ...config import env_fallback_platform  # noqa: F401 — used below

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
            host = typer.prompt(f"{platform.title()} host", default=default_host)
        if not token:
            token = typer.prompt(f"{platform.title()} token (PAT)", hide_input=True)

    assert host is not None and token is not None

    try:
        prof, user = do_login(
            profile_name=profile, host=host, token=token,
            registry_host=(registry_host or ""), platform=platform,
        )
    except (AuthError, ProviderError) as e:
        _err(str(e))

    typer.secho("Login OK", fg=typer.colors.GREEN)
    typer.echo(f"Profile : {prof.name}")
    typer.echo(f"Host    : {prof.host}")
    typer.echo(f"User    : {getattr(user, 'username', '-')}")
    typer.echo(f"Config  : {config_path()}")


def whoami():
    """Show current user for the active profile (requires login)."""
    try:
        prof = load_active_profile()
        u = get_provider(prof).whoami()
    except (AuthError, ProviderError) as e:
        _err(str(e))

    typer.echo(f"{u.username} ({u.name}) @ {prof.host}")


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
        typer.echo("No profiles found. Run: dlane login")
        raise typer.Exit(code=1)

    for name in sorted(profiles.keys()):
        mark = "*" if name == active else " "
        p = profiles.get(name, {})
        host = p.get("host", "")
        typer.echo(f"{mark} {name}\t{host}")

    typer.echo("")
    typer.echo(f"Active profile: {active}")


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
            typer.secho("No profiles found. Run `dlane login --profile <name>` first.", fg=typer.colors.RED)
            raise typer.Exit(1)

        typer.echo("Available profiles:")
        for n in names:
            typer.echo(f"  - {n}")

        profile = typer.prompt("Profile to activate")

    prof = get_profile(cfg, profile)
    if not prof:
        typer.secho(f"Profile not found: {profile}", fg=typer.colors.RED)
        raise typer.Exit(1)

    set_active_profile_name(cfg, profile)
    save_config(cfg)

    typer.secho("Active profile updated.", fg=typer.colors.GREEN)
    typer.echo(f"active : {profile}")
    typer.echo(f"host   : {prof.host}")
