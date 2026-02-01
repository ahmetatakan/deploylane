from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import os

try:
    import tomllib  # Python 3.11+
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .utils import ensure_dir, chmod_600

APP_DIR_NAME = "deploylane"
DEFAULT_HOST = "https://gitlab.com"


def _config_dir() -> Path:
    """Return the local config directory path."""
    return Path.home() / ".config" / APP_DIR_NAME


def config_path() -> Path:
    """Return the config file path."""
    return _config_dir() / "config.toml"


def _read_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    return tomllib.loads(raw.decode("utf-8"))


def _write_toml(path: Path, data: Dict[str, Any]) -> None:
    """
    Minimal TOML writer to avoid adding a new dependency.
    Supports only the structure we use:
      active_profile = "..."
      [profiles."name"]
      host = "..."
      token = "..."
    """
    lines: list[str] = []

    active = data.get("active_profile")
    if isinstance(active, str) and active.strip():
        lines.append(f'active_profile = "{active.strip()}"')
        lines.append("")

    profiles = data.get("profiles", {})
    if isinstance(profiles, dict):
        for name, p in profiles.items():
            if not isinstance(p, dict):
                continue
            lines.append(f'[profiles."{name}"]')
            for k, v in p.items():
                if v is None:
                    continue
                vv = str(v).replace('"', '\\"')
                lines.append(f'{k} = "{vv}"')
            lines.append("")

    ensure_dir(path.parent)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    chmod_600(path)


@dataclass
class Profile:
    name: str
    host: str
    token: str
    registry_host: str = ""   # Docker registry host (example: registry-gitlab.example.com)


def load_config() -> Dict[str, Any]:
    return _read_toml(config_path())


def save_config(cfg: Dict[str, Any]) -> None:
    _write_toml(config_path(), cfg)


def get_active_profile_name(cfg: Dict[str, Any]) -> str:
    name = cfg.get("active_profile")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "default"


def set_active_profile_name(cfg: Dict[str, Any], name: str) -> None:
    cfg["active_profile"] = name


def upsert_profile(cfg: Dict[str, Any], profile: Profile) -> None:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        cfg["profiles"] = profiles
    profiles[profile.name] = {"host": profile.host, "token": profile.token}


def get_profile(cfg: Dict[str, Any], name: str) -> Optional[Profile]:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict):
        return None
    p = profiles.get(name)
    if not isinstance(p, dict):
        return None

    host = p.get("host")
    token = p.get("token")
    if not isinstance(host, str) or not isinstance(token, str):
        return None
    if not host.strip() or not token.strip():
        return None

    return Profile(name=name, host=host.strip(), token=token.strip())


def delete_profile(cfg: Dict[str, Any], name: str) -> bool:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict):
        return False
    if name in profiles:
        del profiles[name]
        return True
    return False


def normalize_host(host: str) -> str:
    """Normalize host input to a stable URL base (no trailing slash)."""
    h = host.strip()
    if not h:
        return DEFAULT_HOST
    if not (h.startswith("http://") or h.startswith("https://")):
        h = "https://" + h
    while h.endswith("/"):
        h = h[:-1]
    return h


def env_fallback_token() -> str | None:
    """Environment fallback for non-interactive use (CI/CD)."""
    return os.getenv("GITLAB_TOKEN") or os.getenv("GITLAB_PAT") or os.getenv("DLANE_TOKEN")


def env_fallback_host() -> str | None:
    """Environment fallback for non-interactive use (CI/CD)."""
    return os.getenv("GITLAB_HOST") or os.getenv("DLANE_HOST")