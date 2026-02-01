from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .config import (
    Profile,
    load_config,
    save_config,
    get_active_profile_name,
    set_active_profile_name,
    upsert_profile,
    get_profile,
    delete_profile,
    normalize_host,
    env_fallback_host,
    env_fallback_token,
    config_path,
)
from .gitlab import whoami, GitLabError


class AuthError(RuntimeError):
    pass


@dataclass
class AuthContext:
    profile: Profile
    user: Optional[object] = None


def load_active_profile() -> Profile:
    """Load the active profile from config, or fall back to env vars."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)
    if prof:
        return prof

    host = env_fallback_host()
    token = env_fallback_token()
    if host and token:
        return Profile(name=active, host=normalize_host(host), token=token)

    raise AuthError(
        "Not logged in. Run: dlane login\n"
        f"Config: {config_path()}"
    )


def login(profile_name: str, host: str, token: str) -> Tuple[Profile, object]:
    """Verify credentials and persist them as the active profile."""
    p = Profile(name=profile_name, host=normalize_host(host), token=token.strip())
    if not p.token:
        raise AuthError("Token is empty.")

    user = whoami(p.host, p.token)

    cfg = load_config()
    upsert_profile(cfg, p)
    set_active_profile_name(cfg, profile_name)
    save_config(cfg)
    return p, user


def logout(profile_name: str, all_profiles: bool = False) -> int:
    """Remove stored profile(s) from config."""
    cfg = load_config()
    removed = 0

    if all_profiles:
        profiles = cfg.get("profiles")
        if isinstance(profiles, dict):
            removed = len(profiles)
        cfg["profiles"] = {}
        cfg["active_profile"] = "default"
        save_config(cfg)
        return removed

    if delete_profile(cfg, profile_name):
        removed = 1
        if get_active_profile_name(cfg) == profile_name:
            cfg["active_profile"] = "default"
        save_config(cfg)

    return removed


def status() -> dict:
    """Return a small status dict (useful for CLI output)."""
    cfg = load_config()
    active = get_active_profile_name(cfg)
    prof = get_profile(cfg, active)

    result = {
        "active_profile": active,
        "has_profile": prof is not None,
        "host": prof.host if prof else None,
        "config_path": str(config_path()),
    }

    if prof:
        try:
            u = whoami(prof.host, prof.token)
            result["logged_in"] = True
            result["username"] = u.username
            result["name"] = u.name
        except GitLabError:
            result["logged_in"] = False
    else:
        result["logged_in"] = False

    return result