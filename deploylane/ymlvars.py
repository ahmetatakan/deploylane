from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple, Optional
import yaml


DEFAULT_FILE = Path(".deploylane") / "vars.yml"


@dataclass(frozen=True)
class VarSpec:
    value: str
    masked: bool
    protected: bool
    environment_scope: str


def _norm_var(v: Dict[str, Any]) -> VarSpec:
    return VarSpec(
        value=str(v.get("value", "")),
        masked=bool(v.get("masked", False)),
        protected=bool(v.get("protected", False)),
        environment_scope=str(v.get("environment_scope", "*") or "*").strip() or "*",
    )


def _looks_non_secret_key(key: str) -> bool:
    k = (key or "").upper()
    return k.endswith("_URL") or k.endswith("_HOST") or k.endswith("_PORT") or k.startswith("PUBLIC_")


def _safe_value(key: str, spec: VarSpec, show_values: bool) -> str:
    """
    Deterministic redaction policy:
    - show_values => show actual value
    - masked => <masked>
    - non-secret-ish keys => show value (helps diffs)
    - otherwise => <redacted>
    """
    if show_values:
        return spec.value
    if spec.masked:
        return "<masked>"
    if _looks_non_secret_key(key):
        return spec.value
    return "<redacted>"


def _sorted_variables_block(variables: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically order variables and normalize environment_scope defaulting.
    NOTE: supports "variables: { KEY: {..} }" format.
    """
    out: Dict[str, Any] = {}
    for key in sorted(variables.keys(), key=lambda s: str(s).lower()):
        meta = variables.get(key)
        if not isinstance(meta, dict):
            continue
        # normalize environment_scope field deterministically
        env_scope = str(meta.get("environment_scope", "*") or "*").strip() or "*"
        m2 = dict(meta)
        m2["environment_scope"] = env_scope

        # stable order inside meta (YAML keeps insertion order when sort_keys=False)
        meta_order = ["value", "masked", "protected", "environment_scope", "variable_type"]
        stable_meta: Dict[str, Any] = {}
        for mk in meta_order:
            if mk in m2:
                stable_meta[mk] = m2[mk]
        for mk in sorted(m2.keys(), key=lambda s: str(s).lower()):
            if mk not in stable_meta:
                stable_meta[mk] = m2[mk]

        out[str(key)] = stable_meta
    return out


def write_vars_file(path: Path, data: Dict[str, Any]) -> None:
    """
    Write variables YAML deterministically:
    - variables are sorted by key
    - meta keys are ordered
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = dict(data)
    variables = doc.get("variables", {})
    if isinstance(variables, dict):
        doc["variables"] = _sorted_variables_block(variables)

    path.write_text(
        yaml.safe_dump(
            doc,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def read_vars_file(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw) or {}


def demo_template(project: str) -> Dict[str, Any]:
    return {
        "project": project,
        "scope": "*",
        "variables": {
            "EXAMPLE_URL": {
                "value": "https://example.com",
                "masked": False,
                "protected": False,
                "environment_scope": "*",
            },
            "SECRET_TOKEN": {
                "value": "change-me",
                "masked": True,
                "protected": True,
                "environment_scope": "*",
            },
        },
    }


def load_vars_yml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def save_vars_yml(path: Path, data: Dict[str, Any]) -> None:
    # reuse deterministic writer
    write_vars_file(path, data)