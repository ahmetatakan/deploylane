from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml


@dataclass(frozen=True)
class DeployTarget:
    name: str
    host: str
    user: str
    deploy_dir: str

    # stable / deterministic inputs
    app_name: str = "next"
    registry_host: str = ""          # <-- no hardcode
    strategy: str = "plain"
    health_host: str = ""
    env_scope: str = "*"

    # legacy / overrides
    service_blue: str = ""
    service_green: str = ""
    port_blue: int = 0
    port_green: int = 0
    port_plain: int = 0


@dataclass(frozen=True)
class DeploySpec:
    project: str
    targets: Dict[str, DeployTarget]
    default_target: str = ""


def read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML root must be a mapping/dict.")
    return data


def load_deployspec(path: Path) -> DeploySpec:
    raw = read_yaml(path)

    project = raw.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("deploy.yml: missing/invalid 'project'.")

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, dict) or not targets_raw:
        raise ValueError("deploy.yml: missing/invalid 'targets'.")

    def _to_int(x: Any) -> int:
        try:
            return int(x)
        except Exception:
            return 0

    # top-level strategy (fallback for per-target)
    top_strategy = str(raw.get("strategy") or "plain").strip().lower()

    # top-level app section (fallback for per-target)
    app_raw = raw.get("app") or {}
    top_app_name = str(app_raw.get("name") or "").strip()
    top_registry_host = str(app_raw.get("registry_host") or "").strip()

    targets: Dict[str, DeployTarget] = {}
    for name, tr in targets_raw.items():
        if not isinstance(name, str) or not isinstance(tr, dict):
            continue

        host = tr.get("host", "")
        user = tr.get("user", "")
        deploy_dir = tr.get("deploy_dir", "")
        if not (isinstance(host, str) and isinstance(user, str) and isinstance(deploy_dir, str)):
            continue
        if not host.strip() or not user.strip() or not deploy_dir.strip():
            continue

        strategy = tr.get("strategy") or top_strategy
        if not isinstance(strategy, str):
            strategy = top_strategy

        app_name = tr.get("app_name", tr.get("app", "")) or top_app_name or "next"
        if not isinstance(app_name, str) or not app_name.strip():
            app_name = "next"

        registry_host = tr.get("registry_host", "") or top_registry_host
        if not isinstance(registry_host, str):
            registry_host = ""
        registry_host = registry_host.strip()

        env_scope = tr.get("env_scope", "*")
        if not isinstance(env_scope, str) or not env_scope.strip():
            env_scope = "*"

        service_blue = tr.get("service_blue", "")
        service_green = tr.get("service_green", "")
        health_host = tr.get("health_host", "")

        # support both flat (port_blue) and nested (ports.blue/green/plain) formats
        ports_raw = tr.get("ports") or {}
        if isinstance(ports_raw, dict):
            port_blue  = ports_raw.get("blue",  tr.get("port_blue",  0))
            port_green = ports_raw.get("green", tr.get("port_green", 0))
            port_plain = ports_raw.get("plain", ports_raw.get("blue", tr.get("port_plain", tr.get("port_blue", 0))))
        else:
            port_blue  = tr.get("port_blue",  0)
            port_green = tr.get("port_green", 0)
            port_plain = tr.get("port_plain", tr.get("port_blue", 0))

        targets[name] = DeployTarget(
            name=name,
            host=host.strip(),
            user=user.strip(),
            deploy_dir=deploy_dir.strip(),
            app_name=str(app_name).strip(),
            registry_host=registry_host,
            strategy=str(strategy).strip(),
            service_blue=str(service_blue or "").strip(),
            service_green=str(service_green or "").strip(),
            port_blue=_to_int(port_blue),
            port_green=_to_int(port_green),
            port_plain=_to_int(port_plain),
            health_host=str(health_host or "").strip(),
            env_scope=env_scope.strip(),
        )

    if not targets:
        raise ValueError("deploy.yml: no valid targets found under 'targets'.")

    default_target = raw.get("default_target", "")
    if not isinstance(default_target, str):
        default_target = ""

    return DeploySpec(project=project.strip(), targets=targets, default_target=default_target.strip())


def _sanitize_env_value(v: str) -> Tuple[str, bool]:
    if "\n" in v or "\r" in v:
        v2 = v.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
        return v2, True
    return v, False


def write_env_file(path: Path, env: Dict[str, str]) -> Tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)

    sanitized = 0
    lines = []
    for k in sorted(env.keys()):
        v = "" if env[k] is None else str(env[k])
        v, s = _sanitize_env_value(v)
        if s:
            sanitized += 1
        lines.append(f"{k}={v}\n")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)
    return (len(env), sanitized)