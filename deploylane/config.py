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
    return Path.home() / ".config" / APP_DIR_NAME


def config_path() -> Path:
    return _config_dir() / "config.toml"


def _read_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    return tomllib.loads(raw.decode("utf-8"))


def _write_toml(path: Path, data: Dict[str, Any]) -> None:
    """
    Minimal TOML writer (no extra dependency).
    Supports:
      active_profile = "..."
      [profiles."name"]
      host = "..."
      token = "..."
      registry_host = "..."
    """
    lines: list[str] = []

    active = data.get("active_profile")
    if isinstance(active, str) and active.strip():
        lines.append(f'active_profile = "{active.strip()}"')
        lines.append("")

    profiles = data.get("profiles", {})
    if isinstance(profiles, dict):
        for name in sorted(profiles.keys(), key=lambda s: str(s).lower()):
            p = profiles.get(name)
            if not isinstance(p, dict):
                continue
            lines.append(f'[profiles."{name}"]')

            # deterministic key order
            for k in ["host", "token", "registry_host"]:
                if k not in p:
                    continue
                v = p.get(k)
                if v is None:
                    continue
                vv = str(v).replace('"', '\\"')
                lines.append(f'{k} = "{vv}"')

            # write any other keys deterministically
            for k in sorted(p.keys(), key=lambda s: str(s).lower()):
                if k in ("host", "token", "registry_host"):
                    continue
                v = p.get(k)
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
    registry_host: str = ""  # optional
    platform: str = "gitlab"


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

    profiles[profile.name] = {
        "host": profile.host,
        "token": profile.token,
        "registry_host": (profile.registry_host or "").strip(),
        "platform": (profile.platform or "gitlab").strip(),
    }


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

    registry_host = p.get("registry_host", "")
    if not isinstance(registry_host, str):
        registry_host = ""

    platform = p.get("platform", "gitlab")
    if not isinstance(platform, str) or not platform.strip():
        platform = "gitlab"

    return Profile(
        name=name,
        host=host.strip(),
        token=token.strip(),
        registry_host=registry_host.strip(),
        platform=platform.strip(),
    )


def delete_profile(cfg: Dict[str, Any], name: str) -> bool:
    profiles = cfg.get("profiles")
    if not isinstance(profiles, dict):
        return False
    if name in profiles:
        del profiles[name]
        return True
    return False


def normalize_host(host: str) -> str:
    h = host.strip()
    if not h:
        return DEFAULT_HOST
    if not (h.startswith("http://") or h.startswith("https://")):
        h = "https://" + h
    while h.endswith("/"):
        h = h[:-1]
    return h


def env_fallback_token() -> str | None:
    return (
        os.getenv("GITLAB_TOKEN")
        or os.getenv("GITLAB_PAT")
        or os.getenv("GITHUB_TOKEN")
        or os.getenv("DLANE_TOKEN")
    )


def env_fallback_host() -> str | None:
    return os.getenv("GITLAB_HOST") or os.getenv("GITHUB_HOST") or os.getenv("DLANE_HOST")


def env_fallback_platform() -> str | None:
    """Infer platform from environment variables if not set in config."""
    if os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_HOST"):
        return "github"
    if os.getenv("GITLAB_TOKEN") or os.getenv("GITLAB_PAT") or os.getenv("GITLAB_HOST"):
        return "gitlab"
    return os.getenv("DLANE_PLATFORM")


def env_fallback_registry_host() -> str | None:
    # optional convenience
    return os.getenv("GITLAB_REGISTRY_HOST") or os.getenv("DLANE_REGISTRY_HOST")