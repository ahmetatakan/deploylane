#!/usr/bin/env bash
set -euo pipefail

CMD="${1:-}"
DEPLOY_DIR="${2:-}"
TAG="${3:-}"              # prepare: sha-...
REGISTRY_USER="${4:-}"
REGISTRY_PASS="${5:-}"

die(){ echo "ERROR: $*" >&2; exit 1; }

[ -n "$DEPLOY_DIR" ] || die "missing deploy_dir"

# DEPLOY_DIR changes -> keep .env path correct
env_file() { echo "$DEPLOY_DIR/.env"; }

# Deterministic binary paths (per your server output)
BIN_LN="/usr/bin/ln"
BIN_SYSTEMCTL="/usr/bin/systemctl"
BIN_NGINX="/usr/sbin/nginx"
BIN_CURL="/usr/bin/curl"

compose_project() {
  local p
  p="$(basename "$DEPLOY_DIR")"
  [[ "$p" =~ [[:space:]] ]] && die "DEPLOY_DIR basename contains whitespace: $p"
  echo "$p"
}

compose() {
  local project
  project="$(compose_project)"
  docker compose --project-directory "$DEPLOY_DIR" -p "$project" --env-file "$(env_file)" "$@"
}

get_env_val() {
  # usage: get_env_val KEY FILE
  local k="$1" f="$2"
  grep -E "^${k}=" "$f" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

ensure_newline_eof() {
  local f="$1"
  [ -f "$f" ] || return 0
  # dosya boş değilse ve son byte newline değilse newline ekle
  if [ -s "$f" ] && [ "$(tail -c 1 "$f" 2>/dev/null || true)" != $'\n' ]; then
    printf '\n' >> "$f"
  fi
}

set_env_kv() {
  local k="$1" v="$2" f="$3"
  ensure_newline_eof "$f"

  if grep -q "^${k}=" "$f" 2>/dev/null; then
    # Use | as delimiter to handle values containing / (URLs, paths, etc.)
    local escaped_v
    escaped_v="$(printf '%s' "$v" | sed 's|[|\\&]|\\&|g')"
    sed -i "s|^${k}=.*|${k}=${escaped_v}|" "$f"
  else
    printf '%s=%s\n' "$k" "$v" >> "$f"
  fi
}

upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }

# Derive tag key name deterministically from APP_NAME + COLOR
tag_key_for() {
  # usage: tag_key_for app_name color
  local app="$1" color="$2"
  echo "$(upper "$app")_TAG_$(upper "$color")"
}

require_sudo_nopasswd() {
  # check with allowed command (sudo -n true can be misleading)
  sudo -n "$BIN_NGINX" -t >/dev/null 2>&1 || die "sudo NOPASSWD not working for nginx -t (check /etc/sudoers.d/nginx-bg-switch)"
}

nginx_switch() {
  local color="$1"

  # read NGINX_DIR from env if present (default snippets)
  local nginx_dir
  nginx_dir="$(get_env_val NGINX_DIR "$(env_file)")"
  [ -n "${nginx_dir:-}" ] || nginx_dir="/etc/nginx/snippets"

  require_sudo_nopasswd
  sudo -n "$BIN_LN" -sf "${nginx_dir}/${APP_NAME}-upstream-${color}.conf" "${nginx_dir}/${APP_NAME}-upstream.conf"
  sudo -n "$BIN_NGINX" -t
  sudo -n "$BIN_SYSTEMCTL" reload nginx
}

# ─── Stack-aware helpers ──────────────────────────────────────────────────────
# STACK is read from .env (injected by dlane deploy render via stacks.py).
# Falls back to "docker" for backwards compatibility with old deploy.yml files.

_read_stack() {
  local s
  s="$(get_env_val STACK "$(env_file)" 2>/dev/null || true)"
  echo "${s:-docker}"
}

stack_health_path() {
  # Returns the default health check path for the current stack.
  # Override by setting HEALTH_PATH in vars.yml.
  local stack="$1"
  case "$stack" in
    fastapi|node) echo "/health"      ;;
    odoo)         echo "/web/health"  ;;
    *)            echo "/"            ;;
  esac
}

stack_max_health_attempts() {
  # Odoo can take longer to boot (DB init). All others: 60s.
  local stack="$1"
  case "$stack" in
    odoo) echo "120" ;;
    *)    echo "60"  ;;
  esac
}

stack_post_start_hook() {
  # Called after container starts, before healthcheck.
  local stack="$1"
  case "$stack" in
    fastapi) echo "Stack: fastapi — waiting 1s for uvicorn workers"; sleep 1 ;;
    odoo)    echo "Stack: odoo — waiting 3s for DB init"; sleep 3             ;;
    *)       : ;;
  esac
}

do_healthcheck() {
  # do_healthcheck <port> <env_file>
  # Uses HEALTH_PATH from .env if set; otherwise uses stack default.
  local port="$1"
  local ef="$2"
  local stack
  stack="$(_read_stack)"

  local health_path
  health_path="$(get_env_val HEALTH_PATH "$ef" 2>/dev/null || true)"
  [ -n "${health_path:-}" ] || health_path="$(stack_health_path "$stack")"

  local max_attempts
  max_attempts="$(stack_max_health_attempts "$stack")"

  echo "=== healthcheck http://127.0.0.1:${port}${health_path} (STACK=${stack}) ==="
  stack_post_start_hook "$stack"

  local ok=0
  for i in $(seq 1 "$max_attempts"); do
    if "$BIN_CURL" -fsS "http://127.0.0.1:${port}${health_path}" >/dev/null; then ok=1; break; fi
    sleep 1
  done
  return $((1 - ok))
}
# ─────────────────────────────────────────────────────────────────────────────

case "$CMD" in
  prepare)
    [ -n "$TAG" ] || die "usage: $0 prepare [deploy_dir] <sha-tag> <registry_user> <registry_pass>"
    [ -n "$REGISTRY_USER" ] || die "missing registry_user"
    [ -n "$REGISTRY_PASS" ] || die "missing registry_pass"
    [[ "$TAG" =~ ^sha-[0-9a-f]{40}$ ]] || die "invalid TAG format: $TAG (expected sha-<40hex>)"

    cd "$DEPLOY_DIR" || die "cannot cd $DEPLOY_DIR"
    test -f docker-compose.yml || die "docker-compose.yml not found in $(pwd)"

    ENV="$(env_file)"
    touch "$ENV"

    # Read APP_NAME early (so we can generate keys deterministically)
    APP_NAME="$(get_env_val APP_NAME "$ENV")"
    [ -n "${APP_NAME:-}" ] || APP_NAME="next"

    # strategy (opt-in plain)
    STRATEGY="$(get_env_val DEPLOY_STRATEGY "$ENV")"
    [ -n "${STRATEGY:-}" ] || STRATEGY="bluegreen"

    # Blue/Green color names (optional overrides; defaults stay "blue"/"green")
    BLUE_NAME="$(get_env_val APP_COLOR_BLUE "$ENV")";   [ -n "${BLUE_NAME:-}" ] || BLUE_NAME="blue"
    GREEN_NAME="$(get_env_val APP_COLOR_GREEN "$ENV")"; [ -n "${GREEN_NAME:-}" ] || GREEN_NAME="green"

    # Ensure ACTIVE_COLOR exists (for bluegreen flow)
    grep -q '^ACTIVE_COLOR=' "$ENV" || echo "ACTIVE_COLOR=${BLUE_NAME}" >> "$ENV"

    # Ensure deterministic tag keys exist (derived from APP_NAME)
    KEY_BLUE="$(tag_key_for "$APP_NAME" "$BLUE_NAME")"
    KEY_GREEN="$(tag_key_for "$APP_NAME" "$GREEN_NAME")"
    grep -q "^${KEY_BLUE}="  "$ENV" || echo "${KEY_BLUE}=$TAG" >> "$ENV"
    grep -q "^${KEY_GREEN}=" "$ENV" || echo "${KEY_GREEN}=$TAG" >> "$ENV"

    # Ensure registry host exists
    grep -q '^REGISTRY_HOST=' "$ENV" || echo 'REGISTRY_HOST=registry-gitlab.sachane.com' >> "$ENV"

    # COMMON: registry host normalize + login will be used by both strategies
    REGISTRY_HOST="$(get_env_val REGISTRY_HOST "$ENV")"
    if [ -z "${REGISTRY_HOST:-}" ]; then
      REGISTRY_HOST="registry-gitlab.sachane.com"
      set_env_kv "REGISTRY_HOST" "$REGISTRY_HOST" "$ENV"
    fi

    REGISTRY_HOST="${REGISTRY_HOST#https://}"
    REGISTRY_HOST="${REGISTRY_HOST#http://}"
    REGISTRY_HOST="${REGISTRY_HOST%/}"
    [ -n "${REGISTRY_HOST:-}" ] || die "REGISTRY_HOST resolved empty (check .env REGISTRY_HOST)"
    set_env_kv "REGISTRY_HOST" "$REGISTRY_HOST" "$ENV"

    echo "Logging into registry: $REGISTRY_HOST as $REGISTRY_USER"
    echo "$REGISTRY_PASS" | docker login "$REGISTRY_HOST" -u "$REGISTRY_USER" --password-stdin

    # Cache compose service list (deterministic source of truth)
    SERVICES="$(compose config --services 2>/dev/null || true)"

    service_name_for() {
      # usage: service_name_for app_name color
      local app="$1" color="$2"
      local a="${app}_${color}"
      local b="${app}-${color}"

      # Prefer underscore if exists, else dash if exists, else underscore fallback
      if echo "$SERVICES" | grep -qx "$a"; then echo "$a"; return; fi
      if echo "$SERVICES" | grep -qx "$b"; then echo "$b"; return; fi
      echo "$a"
    }

    if [ "$STRATEGY" = "plain" ]; then
      # Plain deploy (single service; no blue/green; no nginx switch)
      PLAIN_SERVICE="$(get_env_val PLAIN_SERVICE "$ENV")"
      PLAIN_TAG_KEY="$(get_env_val PLAIN_TAG_KEY "$ENV")"
      PLAIN_PORT="$(get_env_val PLAIN_PORT "$ENV")"

      # Deterministic defaults if not set:
      [ -n "${PLAIN_SERVICE:-}" ] || PLAIN_SERVICE="$APP_NAME"
      [ -n "${PLAIN_TAG_KEY:-}" ] || PLAIN_TAG_KEY="$(upper "$APP_NAME")_TAG"
      [ -n "${PLAIN_PORT:-}" ] || PLAIN_PORT="3000"

      set_env_kv "$PLAIN_TAG_KEY" "$TAG" "$ENV"

      echo "STRATEGY=plain -> SERVICE=$PLAIN_SERVICE TAG_KEY=$PLAIN_TAG_KEY TAG=$TAG PORT=$PLAIN_PORT"
      echo "Using compose project: $(compose_project)"

      echo "=== .env ==="
      cat "$ENV"

      echo "=== resolved images (compose config) ==="
      compose config | sed -n 's/^[[:space:]]*image:[[:space:]]*/image: /p'

      if ! compose config | awk '/^[[:space:]]*image:/{print $2}' | grep -q ":${TAG}$"; then
        echo "FATAL: compose did not resolve image tag to ${TAG}"
        exit 1
      fi

      compose up -d --no-deps --pull always --force-recreate "$PLAIN_SERVICE"

      cid="$(compose ps -q "$PLAIN_SERVICE")"
      [ -n "${cid:-}" ] || die "No container id for $PLAIN_SERVICE"

      echo "=== running container ==="
      docker inspect --format '{{.Name}} {{.State.Status}} {{.Config.Image}} {{.Created}}' "$cid"

      if ! docker inspect --format '{{.Config.Image}}' "$cid" | grep -q ":${TAG}$"; then
        echo "FATAL: running container image is not tagged with ${TAG}"
        exit 1
      fi

      do_healthcheck "$PLAIN_PORT" "$ENV" || (echo "Healthcheck failed for $PLAIN_SERVICE" && compose logs --tail=200 "$PLAIN_SERVICE" && exit 1)

      echo "READY. (plain) No nginx switch required."
      exit 0
    fi

    # --- blue/green behavior (deterministic via APP_NAME) ---
    ACTIVE_COLOR="$(get_env_val ACTIVE_COLOR "$ENV")"
    [ -n "${ACTIVE_COLOR:-}" ] || ACTIVE_COLOR="$BLUE_NAME"

    if [ "$ACTIVE_COLOR" = "$GREEN_NAME" ]; then
      TARGET="$BLUE_NAME"
    else
      TARGET="$GREEN_NAME"
    fi

    SERVICE="$(service_name_for "$APP_NAME" "$TARGET")"
    KEY="$(tag_key_for "$APP_NAME" "$TARGET")"

    if [ "$TARGET" = "$BLUE_NAME" ]; then
      PORT="$(get_env_val APP_PORT_BLUE "$ENV")"; [ -n "${PORT:-}" ] || PORT="3001"
    else
      PORT="$(get_env_val APP_PORT_GREEN "$ENV")"; [ -n "${PORT:-}" ] || PORT="3002"
    fi

    echo "ACTIVE=$ACTIVE_COLOR -> PREPARE TARGET=$TARGET SERVICE=$SERVICE TAG=$TAG (APP_NAME=$APP_NAME)"
    echo "Using compose project: $(compose_project)"

    set_env_kv "$KEY" "$TAG" "$ENV"

    echo "=== .env ==="
    cat "$ENV"

    echo "=== resolved images (compose config) ==="
    compose config | sed -n 's/^[[:space:]]*image:[[:space:]]*/image: /p'

    if ! compose config | awk '/^[[:space:]]*image:/{print $2}' | grep -q ":${TAG}$"; then
      echo "FATAL: compose did not resolve image tag to ${TAG}"
      exit 1
    fi

    compose up -d --no-deps --pull always --force-recreate "$SERVICE"

    cid="$(compose ps -q "$SERVICE")"
    [ -n "${cid:-}" ] || die "No container id for $SERVICE"

    echo "=== running container ==="
    docker inspect --format '{{.Name}} {{.State.Status}} {{.Config.Image}} {{.Created}}' "$cid"

    if ! docker inspect --format '{{.Config.Image}}' "$cid" | grep -q ":${TAG}$"; then
      echo "FATAL: running container image is not tagged with ${TAG}"
      exit 1
    fi

    do_healthcheck "$PORT" "$ENV" || (echo "Healthcheck failed for $SERVICE" && compose logs --tail=200 "$SERVICE" && exit 1)

    echo "READY. Switch traffic with:"
    echo "  $0 switch $DEPLOY_DIR $TARGET"
    ;;

  switch)
    DEPLOY_DIR="${2:-}"
    COLOR="${3:-}"
    [ -n "$DEPLOY_DIR" ] || die "missing deploy_dir"

    ENV="$(env_file)"
    test -f "$ENV" || die ".env not found in $DEPLOY_DIR"

    APP_NAME="$(get_env_val APP_NAME "$ENV")"
    [ -n "${APP_NAME:-}" ] || APP_NAME="next"

    BLUE_NAME="$(get_env_val APP_COLOR_BLUE "$ENV")";   [ -n "${BLUE_NAME:-}" ] || BLUE_NAME="blue"
    GREEN_NAME="$(get_env_val APP_COLOR_GREEN "$ENV")"; [ -n "${GREEN_NAME:-}" ] || GREEN_NAME="green"

    [ -n "${COLOR:-}" ] || die "usage: $0 switch [deploy_dir] ${BLUE_NAME}|${GREEN_NAME}"
    [ "$COLOR" = "$BLUE_NAME" ] || [ "$COLOR" = "$GREEN_NAME" ] || die "usage: $0 switch [deploy_dir] ${BLUE_NAME}|${GREEN_NAME}"

    STRATEGY="$(get_env_val DEPLOY_STRATEGY "$ENV")"
    [ -n "${STRATEGY:-}" ] || STRATEGY="bluegreen"
    if [ "$STRATEGY" = "plain" ]; then
      echo "INFO: DEPLOY_STRATEGY=plain → switch is not applicable. Nothing to do."
      exit 0
    fi

    nginx_switch "$COLOR"
    set_env_kv "ACTIVE_COLOR" "$COLOR" "$ENV" || true
    echo "SWITCHED to $COLOR"
    ;;

  auto-switch)
    DEPLOY_DIR="${2:-}"
    [ -n "$DEPLOY_DIR" ] || die "missing deploy_dir"

    ENV="$(env_file)"
    test -f "$ENV" || die ".env not found in $DEPLOY_DIR"

    APP_NAME="$(get_env_val APP_NAME "$ENV")"
    [ -n "${APP_NAME:-}" ] || APP_NAME="next"

    STRATEGY="$(get_env_val DEPLOY_STRATEGY "$ENV")"
    [ -n "${STRATEGY:-}" ] || STRATEGY="bluegreen"
    if [ "$STRATEGY" = "plain" ]; then
      echo "INFO: DEPLOY_STRATEGY=plain → auto-switch is not applicable. Nothing to do."
      exit 0
    fi

    BLUE_NAME="$(get_env_val APP_COLOR_BLUE "$ENV")";   [ -n "${BLUE_NAME:-}" ] || BLUE_NAME="blue"
    GREEN_NAME="$(get_env_val APP_COLOR_GREEN "$ENV")"; [ -n "${GREEN_NAME:-}" ] || GREEN_NAME="green"

    ACTIVE_COLOR="$(get_env_val ACTIVE_COLOR "$ENV")"
    [ -n "${ACTIVE_COLOR:-}" ] || ACTIVE_COLOR="$BLUE_NAME"

    if [ "$ACTIVE_COLOR" = "$GREEN_NAME" ]; then
      TARGET="$BLUE_NAME"; OLD="$GREEN_NAME"
      PORT="$(get_env_val APP_PORT_BLUE "$ENV")"; [ -n "${PORT:-}" ] || PORT="3001"
    else
      TARGET="$GREEN_NAME"; OLD="$BLUE_NAME"
      PORT="$(get_env_val APP_PORT_GREEN "$ENV")"; [ -n "${PORT:-}" ] || PORT="3002"
    fi

    echo "AUTO-SWITCH: active=$ACTIVE_COLOR -> target=$TARGET (rollback=$OLD)"

    "$0" switch "$DEPLOY_DIR" "$TARGET"

    do_healthcheck "$PORT" "$ENV"; ok=$?

    HOST="$(get_env_val HEALTH_HOST "$ENV")"

    ok2=1
    if [ -n "${HOST:-}" ]; then
      echo "Post-switch nginx check (Host): http://127.0.0.1/ (Host: $HOST)"
      ok2=0
      for i in $(seq 1 10); do
        if "$BIN_CURL" -fsS -H "Host: $HOST" "http://127.0.0.1/" >/dev/null; then ok2=1; break; fi
        sleep 1
      done
    fi

    if [ "$ok" -ne 1 ] || [ "$ok2" -ne 1 ]; then
      echo "Post-switch failed -> ROLLBACK to $OLD"
      "$0" switch "$DEPLOY_DIR" "$OLD"
      die "auto-switch failed; rolled back to $OLD"
    fi

    echo "AUTO-SWITCH OK -> now $TARGET"
    ;;

  *)
    echo "Usage:"
    echo "  $0 prepare     [deploy_dir] <sha-tag> <registry_user> <registry_pass>"
    echo "  $0 switch      [deploy_dir] <color>"
    echo "  $0 auto-switch [deploy_dir]"
    exit 1
    ;;
esac