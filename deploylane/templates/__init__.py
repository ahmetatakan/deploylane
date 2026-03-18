from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


TEMPLATES_DIR = Path(__file__).parent


def _render(template_path: Path, ctx: Dict[str, str]) -> str:
    text = template_path.read_text(encoding="utf-8")
    for key, value in ctx.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def render_nginx_blue(ctx: Dict[str, str]) -> str:
    return _render(TEMPLATES_DIR / "nginx" / "upstream-blue.conf", ctx)


def render_nginx_green(ctx: Dict[str, str]) -> str:
    return _render(TEMPLATES_DIR / "nginx" / "upstream-green.conf", ctx)


def render_nginx_site_include(ctx: Dict[str, str]) -> str:
    return _render(TEMPLATES_DIR / "nginx" / "sites-include.conf", ctx)


def render_sudoers(ctx: Dict[str, str]) -> str:
    return _render(TEMPLATES_DIR / "sudoers" / "nginx-bg-switch", ctx)


def render_install_sh(ctx: Dict[str, str]) -> str:
    return _render(TEMPLATES_DIR / "install.sh", ctx)


def render_compose(strategy: str, ctx: Dict[str, str]) -> str:
    name = f"{strategy}.yml"
    path = TEMPLATES_DIR / "compose" / name
    if not path.exists():
        path = TEMPLATES_DIR / "compose" / "plain.yml"
    return _render(path, ctx)


def render_gitlab_ci(ctx: Dict[str, Any], targets: Dict[str, Any], strategy_top: str = "plain") -> str:
    """Generate .gitlab-ci.yml from deploy.yml targets.

    - One deploy job per target.
    - prod/production target → rules: main branch + resource_group.
    - Other targets → rules: MR event.
    - Bluegreen targets → deploy.sh prepare + auto-switch.
    - Plain targets → deploy.sh prepare only.
    """
    app_name = ctx["app_name"]

    lines: list[str] = [
        "stages: [build, deploy]",
        "",
        "variables:",
        '  DOCKER_BUILDKIT: "1"',
        "",
        ".ssh_setup: &ssh_setup",
        "  - set -euo pipefail",
        '  - test -n "$PROD_HOST" || (echo "missing PROD_HOST" && exit 1)',
        '  - test -n "$PROD_SSH_KEY" || (echo "missing PROD_SSH_KEY" && exit 1)',
        "  - apk add --no-cache openssh-client",
        "  - mkdir -p ~/.ssh",
        "  - (umask 077; printf '%s' \"$PROD_SSH_KEY\" | base64 -d > ~/.ssh/id_ed25519)",
        "  - chmod 600 ~/.ssh/id_ed25519",
        "  - ssh-keygen -y -f ~/.ssh/id_ed25519 >/dev/null",
        '  - ssh-keyscan -T 10 -H "$PROD_HOST" >> ~/.ssh/known_hosts',
        "",
        "build:",
        "  stage: build",
        "  image: docker:27",
        "  services:",
        "    - name: docker:27-dind",
        "      alias: docker",
        '      command: ["--tls=false"]',
        "  variables:",
        '    DOCKER_HOST: "tcp://docker:2375"',
        '    DOCKER_TLS_CERTDIR: ""',
        '    DOCKER_DRIVER: "overlay2"',
        '    DOCKER_BUILDKIT: "1"',
        "  interruptible: true",
        "  script:",
        "    - set -euo pipefail",
        "    - for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break || true; echo \"waiting docker... ($i)\"; sleep 1; done",
        "    - docker info",
        '    - echo "$CI_REGISTRY_PASSWORD" | docker login -u "$CI_REGISTRY_USER" --password-stdin "$CI_REGISTRY"',
        '    - TAG="sha-$CI_COMMIT_SHA"',
        '    - IMAGE_REPO="$CI_REGISTRY/$CI_PROJECT_PATH"',
        '    - IMAGE_REF="$IMAGE_REPO:$TAG"',
        '    - CACHE_REF="$IMAGE_REPO:buildcache"',
        "    - docker buildx create --use --name bx --driver docker-container || docker buildx use bx",
        "    - docker buildx inspect --bootstrap",
        "    - |",
        "      docker buildx build \\",
        "        --platform linux/amd64 \\",
        "        --provenance=false \\",
        "        --sbom=false \\",
        '        --tag "$IMAGE_REF" \\',
        '        --cache-from type=registry,ref="$CACHE_REF" \\',
        '        --cache-to type=registry,ref="$CACHE_REF",mode=max \\',
        "        --push \\",
        "        .",
        "  rules:",
        "    - if: '$CI_PIPELINE_SOURCE == \"merge_request_event\"'",
        "    - if: '$CI_COMMIT_BRANCH == \"main\"'",
    ]

    for t_name, t_data in targets.items():
        if not isinstance(t_data, dict):
            continue

        t_strategy = str(t_data.get("strategy") or strategy_top).strip() or "plain"
        t_upper = t_name.upper()
        is_primary = t_name in ("prod", "production")
        deploy_dir_var = f"${t_upper}_DEPLOY_DIR"
        env_name = "production" if is_primary else t_name

        deploy_script = (
            f'      ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes "$PROD_USER@$PROD_HOST" \\\n'
            f'      "set -euo pipefail; DEPLOY_DIR=\'{deploy_dir_var}\'; \\\n'
            f'       cd \\"$DEPLOY_DIR\\"; \\\n'
            f"       test -f ./deploy.sh || (echo 'deploy.sh missing' && exit 1); \\\n"
            f"       chmod +x ./deploy.sh; \\\n"
            f'       ./deploy.sh prepare \\"$DEPLOY_DIR\\" \'$TAG\' \'$REGISTRY_USER\' \'$REGISTRY_PASS\''
        )
        if t_strategy == "bluegreen" and is_primary:
            deploy_script += '; \\\n       ./deploy.sh auto-switch \\"$DEPLOY_DIR\\"'
        deploy_script += '"'

        lines += [
            "",
            f"deploy_{t_name}:",
            "  stage: deploy",
            "  image: alpine:3.20",
            '  needs: ["build"]',
            "  interruptible: true",
        ]
        if is_primary:
            lines.append(f"  resource_group: prod-{app_name}")
        lines += [
            "  environment:",
            f"    name: {env_name}",
            f"    url: \"\"  # fill in: https://your-domain.com",
            "  before_script:",
            "    - *ssh_setup",
            "  script:",
            "    - set -euo pipefail",
            f'    - test -n "$PROD_USER" || (echo "missing PROD_USER" && exit 1)',
            f'    - test -n "{deploy_dir_var}" || (echo "missing {t_upper}_DEPLOY_DIR" && exit 1)',
            f'    - test -n "$REGISTRY_USER" || (echo "missing REGISTRY_USER" && exit 1)',
            f'    - test -n "$REGISTRY_PASS" || (echo "missing REGISTRY_PASS" && exit 1)',
            f'    - TAG="sha-$CI_COMMIT_SHA"',
            f'    - echo "Deploying {t_upper} TAG=$TAG"',
            "    - |",
            deploy_script,
            "  rules:",
        ]
        if is_primary:
            lines.append("    - if: '$CI_COMMIT_BRANCH == \"main\"'")
        else:
            lines.append("    - if: '$CI_PIPELINE_SOURCE == \"merge_request_event\"'")
            lines.append("      when: on_success")

    lines.append("")
    return "\n".join(lines)
