from __future__ import annotations

import re
import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import typer
import yaml

from ..workspace import load_workspace
from ._utils import _err
from .workspace._utils import _resolve_ws

# ─── Finding model ─────────────────────────────────────────────────────────────

OK    = "ok"
WARN  = "warn"
ERROR = "error"

# GitLab built-in variables — never expected in vars.yml
_CI_BUILTINS = re.compile(r'^CI_|^GITLAB_|^FF_')


@dataclass
class Finding:
    level: str    # ok | warn | error
    section: str  # "local" or "remote:<target>"
    label: str
    message: str


# ─── Local checks ─────────────────────────────────────────────────────────────

_PLACEHOLDER_HINTS = ("fill in", "your server", "example.com", "0.0.0.0", "x.x.x.x")


def _check_deploy_yml(base: Path, findings: List[Finding]) -> Optional[dict]:
    deploy_yml = base / "deploy.yml"
    if not deploy_yml.exists():
        findings.append(Finding(ERROR, "local", "deploy.yml", "not found — run: dlane sync"))
        return None

    try:
        raw = yaml.safe_load(deploy_yml.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        findings.append(Finding(ERROR, "local", "deploy.yml", f"parse error: {exc}"))
        return None

    issues: List[str] = []

    if not str(raw.get("project") or "").strip():
        issues.append("'project' field is empty")

    targets = raw.get("targets") or {}
    if not targets:
        issues.append("no targets defined")

    default_target = str(raw.get("default_target") or "").strip()
    if default_target and default_target not in targets:
        issues.append(f"default_target '{default_target}' not in targets")

    for t_name, t_data in targets.items():
        if not isinstance(t_data, dict):
            continue
        for f_name in ("host", "user", "deploy_dir"):
            val = str(t_data.get(f_name) or "").strip()
            if not val:
                issues.append(f"targets.{t_name}.{f_name} is empty")
            elif any(h in val.lower() for h in _PLACEHOLDER_HINTS):
                issues.append(f"targets.{t_name}.{f_name} looks like a placeholder: '{val}'")

    if issues:
        for issue in issues:
            findings.append(Finding(ERROR, "local", "deploy.yml", issue))
    else:
        n = len(targets)
        findings.append(Finding(OK, "local", "deploy.yml", f"valid ({n} target{'s' if n != 1 else ''})"))

    return raw


def _check_compose(base: Path, raw: dict, app_name: str, findings: List[Finding]) -> None:
    strategy_top = str(raw.get("strategy") or "plain").strip() or "plain"
    targets = raw.get("targets") or {}

    strategies: set = set()
    for t_data in targets.values():
        if isinstance(t_data, dict):
            s = str(t_data.get("strategy") or strategy_top).strip() or "plain"
            strategies.add(s)
    if not strategies:
        strategies.add(strategy_top)

    compose_dir = base / "compose"
    for strategy in sorted(strategies):
        compose_file = compose_dir / f"{strategy}.yml"
        label = f"compose/{strategy}.yml"

        if not compose_file.exists():
            findings.append(Finding(ERROR, "local", label, "not found — run: dlane sync"))
            continue

        try:
            compose_raw = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            findings.append(Finding(ERROR, "local", label, f"parse error: {exc}"))
            continue

        services = list((compose_raw.get("services") or {}).keys())
        expected = (
            {f"{app_name}_blue", f"{app_name}_green"}
            if strategy == "bluegreen"
            else {app_name}
        )
        missing = expected - set(services)
        if missing:
            findings.append(Finding(WARN, "local", label,
                f"expected service(s) not found: {', '.join(sorted(missing))} "
                f"(found: {', '.join(services) or 'none'})"))
        else:
            findings.append(Finding(OK, "local", label,
                f"valid  (services: {', '.join(sorted(services))})"))


def _check_env_files(base: Path, raw: dict, app_name: str, findings: List[Finding]) -> None:
    strategy_top = str(raw.get("strategy") or "plain").strip() or "plain"
    targets = raw.get("targets") or {}
    tag_key = app_name.upper().replace("-", "_") + "_TAG"
    env_dir = base / "env"

    for t_name, t_data in targets.items():
        if not isinstance(t_data, dict):
            continue

        label = f"env/{t_name}.env"
        env_file = env_dir / f"{t_name}.env"

        if not env_file.exists():
            findings.append(Finding(ERROR, "local", label, "not found — run: dlane sync"))
            continue

        kvs: Dict[str, str] = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                kvs[k.strip()] = v

        strategy = str(t_data.get("strategy") or strategy_top).strip() or "plain"
        required = {"APP_NAME", "DEPLOY_STRATEGY"}
        if strategy == "bluegreen":
            required |= {"ACTIVE_COLOR", f"{tag_key}_BLUE", f"{tag_key}_GREEN"}
        else:
            required |= {tag_key}

        missing = required - set(kvs.keys())
        if missing:
            findings.append(Finding(WARN, "local", label,
                f"missing keys: {', '.join(sorted(missing))}"))
        else:
            findings.append(Finding(OK, "local", label,
                f"{len(kvs)} keys, required keys present"))


def _check_deploy_sh(base: Path, findings: List[Finding]) -> None:
    sh = base / "scripts" / "deploy.sh"
    if sh.exists():
        findings.append(Finding(OK, "local", "scripts/deploy.sh", "exists"))
    else:
        findings.append(Finding(ERROR, "local", "scripts/deploy.sh",
            "not found — run: dlane sync"))


def _check_vars_yml(base: Path, findings: List[Finding]) -> Optional[Dict]:
    vars_yml = base / "vars.yml"
    if not vars_yml.exists():
        findings.append(Finding(WARN, "local", "vars.yml", "not found (optional)"))
        return None

    try:
        data = yaml.safe_load(vars_yml.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        findings.append(Finding(ERROR, "local", "vars.yml", f"parse error: {exc}"))
        return None

    variables = data.get("variables") or {}
    total = len(variables)
    empty_masked = [
        k for k, v in variables.items()
        if isinstance(v, dict) and v.get("masked") and not str(v.get("value") or "").strip()
    ]

    if empty_masked:
        findings.append(Finding(WARN, "local", "vars.yml",
            f"{total} variables — {len(empty_masked)} masked but empty "
            f"(fill before 'vars apply'): {', '.join(sorted(empty_masked))}"))
    else:
        findings.append(Finding(OK, "local", "vars.yml", f"{total} variables"))

    return variables


def _extract_job_refs(job_data: dict, inline_vars: set, local_shell_vars: set) -> set:
    """Extract $VAR references from a single job's script/before_script/after_script."""
    parts: List[str] = []
    for key in ("script", "before_script", "after_script"):
        val = job_data.get(key)
        if isinstance(val, list):
            parts.extend(str(v) for v in val)
        elif isinstance(val, str):
            parts.append(val)
    text = "\n".join(parts)
    return {
        m for m in re.findall(r'(?<!\\)\$\{?([A-Z][A-Z0-9_]+)\}?', text)
        if not _CI_BUILTINS.match(m)
        and m not in inline_vars
        and m not in local_shell_vars
    }


def _load_check_config(base: Path) -> dict:
    """Load .deploylane/check.yml if it exists. Returns empty dict on any error."""
    cfg_path = base / "check.yml"
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _check_ci_yml(base: Path, vars_data: Optional[Dict], findings: List[Finding]) -> None:
    ci_yml = base / "ci" / ".gitlab-ci.yml"
    label = "ci/.gitlab-ci.yml"

    if not ci_yml.exists():
        findings.append(Finding(WARN, "local", label, "not found (optional)"))
        return

    try:
        content = ci_yml.read_text(encoding="utf-8")
        ci_doc = yaml.safe_load(content) or {}
    except Exception as exc:
        findings.append(Finding(ERROR, "local", label, f"parse error: {exc}"))
        return

    # Inline variables: defined in YAML variables: blocks, not expected in vars.yml
    inline_vars: set = set()
    for block in ci_doc.values():
        if isinstance(block, dict):
            for k in (block.get("variables") or {}).keys():
                inline_vars.add(str(k))
    for k in (ci_doc.get("variables") or {}).keys():
        inline_vars.add(str(k))

    # Shell-local assignments — two patterns:
    #   1. VARNAME= as first token on the line (standard and multiline "|" continuations)
    #   2. case statement branches: "pattern) VARNAME=..." after ")"
    local_shell_vars = set(re.findall(
        r'(?m)^[ \t]*(?:-[ \t]+)?([A-Z][A-Z0-9_]+)=(?!=)', content,
    )) | set(re.findall(
        r'\)\s+([A-Z][A-Z0-9_]+)=(?!=)', content,
    ))

    # Non-job top-level keys to skip when iterating jobs
    _NON_JOB_KEYS = {"stages", "variables", "include", "workflow", "default", "image", "services"}

    # Build job map (only real job entries)
    jobs: Dict[str, dict] = {
        k: v for k, v in ci_doc.items()
        if isinstance(v, dict) and k not in _NON_JOB_KEYS and not k.startswith(".")
    }

    # Find dotenv artifact producers: jobs that write a dotenv artifact
    dotenv_producers: set = set()
    for job_name, job_data in jobs.items():
        artifacts = job_data.get("artifacts") or {}
        if isinstance(artifacts, dict):
            reports = artifacts.get("reports") or {}
            if isinstance(reports, dict) and reports.get("dotenv"):
                dotenv_producers.add(job_name)

    # Find dotenv artifact consumers: jobs that need a producer with artifacts: true
    dotenv_consumers: set = set()
    for job_name, job_data in jobs.items():
        needs = job_data.get("needs") or []
        for need in needs:
            if isinstance(need, dict):
                needed = need.get("job", "")
                if needed in dotenv_producers and need.get("artifacts", True):
                    dotenv_consumers.add(job_name)
            elif isinstance(need, str) and need in dotenv_producers:
                dotenv_consumers.add(job_name)

    # Map each variable reference to the jobs that use it
    var_to_jobs: Dict[str, set] = {}
    for job_name, job_data in jobs.items():
        for var in _extract_job_refs(job_data, inline_vars, local_shell_vars):
            var_to_jobs.setdefault(var, set()).add(job_name)

    # All refs across all jobs
    refs = set(var_to_jobs.keys())

    if vars_data is None:
        findings.append(Finding(WARN, "local", label,
            f"{len(refs)} variable reference(s) — no vars.yml to cross-check against"))
        return

    defined = set(vars_data.keys())
    undefined = refs - defined
    unused    = defined - refs

    # Load user-confirmed artifact vars from check.yml (suppress WARN for these)
    check_cfg = _load_check_config(base)
    confirmed_artifact_vars: set = set(
        str(v) for v in ((check_cfg.get("ci") or {}).get("artifact_vars") or [])
    )

    # Classify undefined vars: artifact-sourced (warn) vs truly missing (error)
    artifact_sourced: List[str] = []
    artifact_confirmed: List[str] = []
    truly_missing:    List[str] = []

    for var in sorted(undefined):
        using_jobs = var_to_jobs.get(var, set())
        # If every job using this var is a dotenv consumer → probably from artifact
        if using_jobs and using_jobs.issubset(dotenv_consumers):
            if var in confirmed_artifact_vars:
                artifact_confirmed.append(var)
            else:
                artifact_sourced.append(var)
        else:
            truly_missing.append(var)

    if truly_missing:
        findings.append(Finding(ERROR, "local", label,
            f"used in CI but missing from vars.yml: {', '.join(truly_missing)}"))
    if artifact_sourced:
        producers_label = ", ".join(sorted(dotenv_producers))
        findings.append(Finding(WARN, "local", label,
            f"likely from dotenv artifact ({producers_label}):  {', '.join(artifact_sourced)}"
            f"  — add to check.yml ci.artifact_vars to suppress"))
    if unused:
        findings.append(Finding(WARN, "local", label,
            f"defined in vars.yml but not referenced in CI: {', '.join(sorted(unused))}"))

    all_ok = not truly_missing and not artifact_sourced and not unused
    if all_ok and not artifact_confirmed:
        findings.append(Finding(OK, "local", label,
            f"{len(refs)} CI variable reference(s), all defined in vars.yml"))
    elif all_ok and artifact_confirmed:
        findings.append(Finding(OK, "local", label,
            f"{len(refs)} CI variable reference(s), all accounted for"
            f"  (artifact: {', '.join(artifact_confirmed)})"))
    elif not truly_missing and not unused:
        findings.append(Finding(OK, "local", label,
            f"{len(refs)} CI variable reference(s), all accounted for"))


# ─── Remote checks ─────────────────────────────────────────────────────────────

_COMPOSE_GENERIC = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.prod.yml",
    "compose.yml",
    "compose.yaml",
]


def _compose_candidates(strategy: str, compose_file: str) -> List[str]:
    """Return ordered candidate filenames to probe on the server.

    Priority: configured name → strategy-specific names → generic fallbacks.
    """
    strategy_specific = [
        f"docker-compose.{strategy}.yml",
        f"docker-compose.{strategy}.yaml",
    ]
    seen: set = {compose_file}
    result = [compose_file]
    for c in strategy_specific + _COMPOSE_GENERIC:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _ssh_test(dest: str, cmd: str) -> str:
    """Run a command over SSH, return stdout. Raises on failure."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", dest, cmd],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def _check_remote_target(
    t_name: str,
    t_raw: dict,
    base: Path,
    app_name: str,
    strategy_top: str,
    findings: List[Finding],
) -> None:
    section = f"remote:{t_name}"

    host       = str(t_raw.get("host")       or "").strip()
    user       = str(t_raw.get("user")       or "deploy").strip()
    remote_dir = str(t_raw.get("deploy_dir") or "").strip()
    strategy   = str(t_raw.get("strategy")   or strategy_top).strip() or "plain"
    compose_file = str(t_raw.get("compose_file") or "docker-compose.yml").strip() or "docker-compose.yml"

    if not host or not remote_dir:
        findings.append(Finding(WARN, section, "SSH",
            "skipped — host or deploy_dir missing in deploy.yml"))
        return

    if not shutil.which("ssh"):
        findings.append(Finding(ERROR, section, "SSH", "ssh not found in PATH"))
        return

    dest = f"{user}@{host}"

    # 1. SSH connectivity
    try:
        _ssh_test(dest, "echo ok")
        findings.append(Finding(OK, section, "SSH", f"connected  ({dest})"))
    except Exception as exc:
        findings.append(Finding(ERROR, section, "SSH",
            f"cannot connect to {dest}: {exc}"))
        return  # no point continuing without SSH

    # 2. deploy_dir existence
    try:
        out = _ssh_test(dest, f"test -d '{remote_dir}' && echo yes || echo no")
        if out == "yes":
            findings.append(Finding(OK, section, "deploy_dir", f"exists  ({remote_dir})"))
        else:
            findings.append(Finding(WARN, section, "deploy_dir",
                f"not found on server  ({remote_dir})  — first deploy?"))
    except Exception as exc:
        findings.append(Finding(WARN, section, "deploy_dir", f"check failed: {exc}"))

    # 3. .env existence + read DEPLOY_STRATEGY (must run before compose discovery)
    env_exists = False
    try:
        out = _ssh_test(dest, f"test -f '{remote_dir}/.env' && echo yes || echo no")
        env_exists = out == "yes"
        if env_exists:
            # Read DEPLOY_STRATEGY from server .env to detect local/remote mismatch
            try:
                env_strategy_out = _ssh_test(
                    dest,
                    f"grep '^DEPLOY_STRATEGY=' '{remote_dir}/.env' 2>/dev/null || true",
                )
                if "=" in env_strategy_out:
                    server_strategy_val = env_strategy_out.partition("=")[2].strip()
                    if server_strategy_val and server_strategy_val != strategy:
                        findings.append(Finding(WARN, section, ".env",
                            f"exists — DEPLOY_STRATEGY={server_strategy_val} on server "
                            f"but deploy.yml says strategy={strategy} "
                            f"— run: dlane restore {app_name.replace('_', '-')}"))
                        strategy = server_strategy_val  # use server truth for remaining checks
                    else:
                        findings.append(Finding(OK, section, ".env",
                            f"exists on server  (DEPLOY_STRATEGY={strategy})"))
                else:
                    findings.append(Finding(OK, section, ".env", "exists on server"))
            except Exception:
                findings.append(Finding(OK, section, ".env", "exists on server"))
        else:
            findings.append(Finding(WARN, section, ".env",
                "not found — run: dlane deploy push"))
    except Exception as exc:
        findings.append(Finding(WARN, section, ".env", f"check failed: {exc}"))

    # 4. Compose file discovery — use actual server strategy
    candidates = _compose_candidates(strategy, compose_file)
    found_compose: Optional[str] = None
    for candidate in candidates:
        try:
            out = _ssh_test(dest, f"test -f '{remote_dir}/{candidate}' && echo yes || echo no")
            if out == "yes":
                found_compose = candidate
                break
        except Exception:
            pass

    if found_compose is None:
        findings.append(Finding(WARN, section, "compose file",
            f"not found on server  (tried: {', '.join(candidates[:3])}) — first deploy?"))
    elif found_compose != compose_file:
        findings.append(Finding(WARN, section, "compose file",
            f"found '{found_compose}' but deploy.yml expects '{compose_file}' "
            f"— update compose_file in deploy.yml"))
    else:
        findings.append(Finding(OK, section, "compose file", f"found  ({found_compose})"))

    # 5. Container / slot state (strategy already reflects server .env)
    tag_key = app_name.upper().replace("-", "_") + "_TAG"
    project_name = remote_dir.rstrip("/").rsplit("/", 1)[-1]

    if strategy == "bluegreen":
        try:
            env_out = _ssh_test(
                dest,
                f"grep -E '^(ACTIVE_COLOR|{tag_key}_BLUE|{tag_key}_GREEN)=' "
                f"'{remote_dir}/.env' 2>/dev/null || true",
            )
            kvs: Dict[str, str] = {}
            for line in env_out.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    kvs[k.strip()] = v.strip()

            active = kvs.get("ACTIVE_COLOR", "")
            tag_b  = kvs.get(f"{tag_key}_BLUE", "")
            tag_g  = kvs.get(f"{tag_key}_GREEN", "")

            if not active:
                findings.append(Finding(WARN, section, "bluegreen state",
                    "ACTIVE_COLOR not set — not deployed yet?"))
            else:
                parts = [f"active={active}"]
                if tag_b:
                    parts.append(f"blue={tag_b}")
                if tag_g:
                    parts.append(f"green={tag_g}")
                findings.append(Finding(OK, section, "bluegreen state",
                    "  ".join(parts)))

                # Verify the active slot's container is actually running
                active_service = f"{app_name}_{active}"
                project_name = remote_dir.rstrip("/").rsplit("/", 1)[-1]
                try:
                    ps_out = _ssh_test(
                        dest,
                        f"docker compose --project-directory '{remote_dir}' "
                        f"-p '{project_name}' ps '{active_service}' 2>/dev/null || true",
                    ).lower()
                    running = "up" in ps_out or "running" in ps_out
                    tag_active = tag_b if active == "blue" else tag_g
                    status = f"tag={tag_active or '—'}"
                    if running:
                        findings.append(Finding(OK, section, "container",
                            f"running  ({active_service}  {status})"))
                    else:
                        findings.append(Finding(WARN, section, "container",
                            f"not running  ({active_service}  {status})"))
                except Exception as exc:
                    findings.append(Finding(WARN, section, "container",
                        f"could not check active slot ({active_service}): {exc}"))
        except Exception as exc:
            findings.append(Finding(WARN, section, "bluegreen state",
                f"could not read remote .env: {exc}"))
    else:
        try:
            ps_out = _ssh_test(
                dest,
                f"docker compose --project-directory '{remote_dir}' -p '{project_name}' "
                f"ps '{app_name}' 2>/dev/null || true",
            ).lower()
            running = "up" in ps_out or "running" in ps_out

            tag_out = _ssh_test(
                dest,
                f"grep '^{tag_key}=' '{remote_dir}/.env' 2>/dev/null || true",
            )
            tag = tag_out.partition("=")[2].strip() if "=" in tag_out else ""

            status = f"tag={tag or '—'}"
            if running:
                findings.append(Finding(OK, section, "container", f"running  ({status})"))
            else:
                findings.append(Finding(WARN, section, "container",
                    f"not running  ({status})"))
        except Exception as exc:
            findings.append(Finding(WARN, section, "container",
                f"could not check: {exc}"))


# ─── Rendering ─────────────────────────────────────────────────────────────────

def _render_section(findings: List[Finding], section: str, title: str) -> int:
    """Print all findings for a section. Returns error count."""
    rows = [f for f in findings if f.section == section]
    if not rows:
        return 0

    typer.secho(f"  {title}", bold=True)
    typer.secho("  " + "─" * 54, fg=typer.colors.BRIGHT_BLACK)

    errors = 0
    for f in rows:
        if f.level == OK:
            icon = typer.style("✓", fg=typer.colors.GREEN)
        elif f.level == WARN:
            icon = typer.style("⚠", fg=typer.colors.YELLOW)
        else:
            icon = typer.style("✗", fg=typer.colors.RED)
            errors += 1
        label = f"{f.label:<28}"
        typer.echo(f"  {icon}  {label}  {f.message}")

    return errors


# ─── Command ───────────────────────────────────────────────────────────────────

def check(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Project alias"),
    target: Optional[str] = typer.Option(None, "--target", help="Limit remote check to this target"),
    remote: bool = typer.Option(False, "--remote", "-r", help="Also validate server state via SSH"),
    file: Optional[Path] = typer.Option(None, "--file", help="workspace.yml path"),
) -> None:
    """Validate project configuration: deploy.yml, compose, env, vars, CI cross-check."""
    if not name:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)

    ws_path = _resolve_ws(file)
    ws = load_workspace(ws_path)
    project = next((p for p in ws.projects if p.name == name), None)
    if not project:
        _err(f"Project '{name}' not found in workspace.")

    ws_dir      = ws_path.parent
    project_path = (ws_dir / project.path).resolve()
    base         = project_path / ".deploylane"
    app_name     = name.replace("-", "_")

    typer.secho(f"▶ CHECK [{name}]", fg=typer.colors.CYAN, bold=True)
    typer.echo("")

    findings: List[Finding] = []

    # Local
    raw = _check_deploy_yml(base, findings)
    if raw:
        _check_deploy_sh(base, findings)
        _check_compose(base, raw, app_name, findings)
        _check_env_files(base, raw, app_name, findings)
        vars_data = _check_vars_yml(base, findings)
        _check_ci_yml(base, vars_data, findings)

    errors = _render_section(findings, "local", "Local")
    typer.echo("")

    # Remote
    if remote and raw:
        targets_raw = raw.get("targets") or {}
        strategy_top = str(raw.get("strategy") or "plain").strip() or "plain"

        to_check = (
            {target: targets_raw[target]}
            if target and target in targets_raw
            else targets_raw
        )

        for t_name, t_raw in to_check.items():
            if not isinstance(t_raw, dict):
                continue
            _check_remote_target(t_name, t_raw, base, app_name, strategy_top, findings)
            remote_errors = _render_section(findings, f"remote:{t_name}", f"Remote [{t_name}]")
            errors += remote_errors
            typer.echo("")

    # Summary
    all_issues = [f for f in findings if f.level in (WARN, ERROR)]
    if not all_issues:
        typer.secho("  ✓  All checks passed.", fg=typer.colors.GREEN, bold=True)
    else:
        errs  = [f for f in all_issues if f.level == ERROR]
        warns = [f for f in all_issues if f.level == WARN]
        parts = []
        if errs:
            parts.append(typer.style(f"{len(errs)} error(s)", fg=typer.colors.RED, bold=True))
        if warns:
            parts.append(typer.style(f"{len(warns)} warning(s)", fg=typer.colors.YELLOW))
        typer.echo("  " + ", ".join(parts))

    if errors:
        raise typer.Exit(code=1)
