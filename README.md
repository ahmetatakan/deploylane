# DeployLane (dlane)

GitLab-focused deployment helper CLI for **deterministic, multi-project CI/CD operations**.

DeployLane bridges your workstation and your servers. It scaffolds server-side deployment files, manages GitLab CI variables, and keeps everything in sync — so your CI pipeline only needs to call `deploy.sh` on the server.

------------------------------------------------------------------------

## How it works

```
workstation (dlane)              GitLab CI Pipeline          Server
─────────────────────            ──────────────────          ──────
dlane scaffold →                                         .env
  deploy.yml      →  dlane deploy push →                docker-compose.yml
  vars.yml                                               deploy.sh
  compose/*.yml   →  dlane deploy install →              nginx config
  nginx/                                                 install.sh (run once)
  ci/.gitlab-ci.yml

dlane vars get/apply → GitLab variables ← Runner uses these
                                            ↓
                                        deploy.sh
```

- **`dlane`** manages server-side files and GitLab variables from your workstation
- **GitLab CI** builds the Docker image and calls `deploy.sh` on the server
- **`deploy.sh`** handles container switching (bluegreen) or restart (plain)

------------------------------------------------------------------------

## Installation

```bash
pip install deploylane
dlane --help
```

------------------------------------------------------------------------

## Quick Start

### 1. Login

```bash
dlane login --host https://gitlab.example.com
# With profile name and registry
dlane login --profile prod --host https://gitlab.example.com --registry-host registry.example.com
```

### 2. Create a workspace

```bash
mkdir ops && cd ops
dlane init                                                    # creates .workspace/workspace.yml
dlane add gateway --gitlab-project acme/gateway --strategy bluegreen
dlane add api     --gitlab-project acme/api     --strategy plain
```

### 3. Scaffold a project

```bash
# Scaffold generates all server-side files from deploy.yml
dlane scaffold gateway

# If vars.yml already has PROD_HOST etc., deploy.yml is pre-filled automatically
dlane vars get gateway          # fetch GitLab variables first
dlane scaffold gateway          # deploy.yml host/user/deploy_dir auto-filled from vars
```

### 4. Push to server & install (once per server)

```bash
dlane deploy push gateway --yes          # .env + docker-compose.yml + deploy.sh → server
dlane deploy install gateway --yes       # nginx + sudoers + install.sh → server, runs install.sh
```

### 5. CI does the rest

Copy `.deploylane/ci/.gitlab-ci.yml` to your project repo. GitLab CI will build, push the image,
and call `deploy.sh` on the server automatically.

------------------------------------------------------------------------

## Deployment strategies

| Strategy    | Description |
|-------------|-------------|
| `bluegreen` | Two containers (blue/green), nginx switches traffic, zero-downtime |
| `plain`     | Single container, `docker compose up -d`, simple restart |

Staging typically uses `plain`; production uses `bluegreen`. Set per-target in `deploy.yml`.

------------------------------------------------------------------------

## deploy.yml

Source of truth for deployment structure. Never overwritten by scaffold after first creation.

```yaml
version: 1
strategy: bluegreen
project: acme/gateway
default_target: prod

app:
  name: gateway
  registry_host: registry.acme.com

targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/gateway
    env_scope: production
    strategy: bluegreen
    health_host: api.acme.com   # optional: nginx health check domain
    ports:
      blue: 8080
      green: 8081

  staging:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/gateway-staging
    env_scope: staging
    strategy: plain             # staging typically uses plain
    ports:
      blue: 8180
      green: 8181
```

------------------------------------------------------------------------

## vars.yml

Stores GitLab CI variable definitions. Used by `vars get/apply/plan/diff`.
Also used by scaffold to pre-fill `deploy.yml` (`PROD_HOST` → `targets.prod.host` etc.).

```yaml
project: acme/gateway
scope: "*"
variables:
  PROD_HOST:
    value: "10.0.0.1"
    masked: false
    protected: false
    environment_scope: "*"
  PROD_SSH_KEY:
    value: "LS0t..."          # base64-encoded private key
    masked: false
    protected: false
    environment_scope: "*"
  PROD_DEPLOY_DIR:
    value: "/home/deploy/gateway"
    masked: true
    protected: false
    environment_scope: "*"
  REGISTRY_USER:
    value: "gitlab+deploy-token-1"
    masked: true
    protected: false
    environment_scope: "*"
  REGISTRY_PASS:
    value: "..."
    masked: true
    protected: false
    environment_scope: "*"
```

**Standard variable naming convention:**

| Variable | Purpose |
|----------|---------|
| `{TARGET}_HOST` | Server IP (e.g. `PROD_HOST`, `STAGING_HOST`) |
| `{TARGET}_USER` | SSH user |
| `{TARGET}_DEPLOY_DIR` | Deploy directory on server |
| `{TARGET}_SSH_KEY` | Base64-encoded SSH private key |
| `REGISTRY_HOST` | Docker registry hostname |
| `REGISTRY_USER` | Registry login user |
| `REGISTRY_PASS` | Registry login password |

------------------------------------------------------------------------

## Scaffold

`dlane scaffold <name>` regenerates server-side files from `deploy.yml`. Safe to re-run.

```bash
dlane scaffold gateway            # regenerate all files
dlane scaffold gateway --force    # also overwrite compose/ and ci/ files
```

**File creation rules:**

| File | Behavior |
|------|---------|
| `deploy.yml` | Created once; never overwritten by scaffold |
| `vars.yml` | Created once with standard placeholders; never overwritten |
| `compose/*.yml` | Created once per strategy; `--force` to regenerate |
| `ci/.gitlab-ci.yml` | Created once; `--force` to regenerate |
| `env/{target}.env` | Always regenerated from deploy.yml |
| `env/{target}.local.env` | Created once (empty); user-managed overrides |
| `nginx/*.conf` | Always regenerated |
| `install.sh` | Always regenerated |
| `deploy.sh` | Always updated |

**vars.yml → deploy.yml sync:**
When scaffold runs and `vars.yml` has non-empty values, it updates `host`, `user`, `deploy_dir`
in `deploy.yml` automatically. To use a custom value, leave the corresponding vars.yml key empty.

**Generated file structure:**

```
.deploylane/
├── deploy.yml                    ← targets, strategy, ports
├── vars.yml                      ← GitLab CI variable definitions
├── scripts/
│   └── deploy.sh                 ← server-side deploy script (bluegreen/plain)
├── compose/
│   ├── bluegreen.yml             ← docker-compose for bluegreen strategy
│   └── plain.yml                 ← docker-compose for plain strategy
├── env/
│   ├── prod.env                  ← base .env template for prod
│   ├── prod.local.env            ← local overrides (gitignored)
│   ├── staging.env               ← base .env template for staging
│   └── staging.local.env         ← local overrides (gitignored)
├── nginx/
│   ├── gateway-upstream-blue.conf
│   ├── gateway-upstream-green.conf
│   └── 00-gateway-upstream.conf
├── sudoers/
│   └── nginx-bg-switch           ← sudoers entry for nginx reload
├── install.sh                    ← one-time server setup script
└── ci/
    └── .gitlab-ci.yml            ← GitLab CI pipeline template
```

------------------------------------------------------------------------

## deploy push

Pushes `.env` + `docker-compose.yml` + `deploy.sh` to the server. Dry run by default.

```bash
dlane deploy push gateway                    # dry run (prints scp commands)
dlane deploy push gateway --yes              # actually push
dlane deploy push gateway --target staging --yes
dlane deploy push gateway --yes --all        # push all workspace projects
dlane deploy push gateway --yes --tag api    # push all projects tagged "api"
```

**What gets pushed:**

| Local file | → Remote |
|-----------|---------|
| `.deploylane/env/prod.env` (+ `prod.local.env` if present) | `{deploy_dir}/.env` |
| `.deploylane/compose/{strategy}.yml` | `{deploy_dir}/docker-compose.yml` |
| `.deploylane/scripts/deploy.sh` | `{deploy_dir}/deploy.sh` |

### .local.env — per-target overrides

```bash
# .deploylane/env/prod.local.env  (gitignored — safe for secrets)
DATABASE_URL=postgres://prod-db:5432/myapp
EXTRA_VAR=custom-value
```

Keys in `.local.env` are merged on top of `{target}.env` before pushing. The file is gitignored automatically.

------------------------------------------------------------------------

## deploy install

One-time server setup. Pushes nginx config, sudoers, install.sh and runs it via SSH.
Prompts for sudo password interactively.

```bash
dlane deploy install gateway                 # dry run
dlane deploy install gateway --yes           # push + run install.sh on server
dlane deploy install gateway --target staging --yes
```

------------------------------------------------------------------------

## deploy history

```bash
dlane deploy history gateway
dlane deploy history gateway --limit 50
```

------------------------------------------------------------------------

## Variables (GitLab)

```bash
dlane vars get gateway            # fetch GitLab variables → vars.yml
dlane vars plan gateway           # preview changes (no write)
dlane vars diff gateway           # diff local vs GitLab
dlane vars apply gateway          # push vars.yml → GitLab
dlane vars prune gateway --yes    # delete GitLab vars not in vars.yml
```

------------------------------------------------------------------------

## Workspace

```bash
# Setup
dlane init                                   # create .workspace/workspace.yml
dlane add <name> --gitlab-project <g/p> --strategy <strategy>
dlane add <name> ... --init                  # add + scaffold in one step
dlane update <name> --strategy bluegreen
dlane remove <name>

# View
dlane list                                   # list all projects
dlane list --tag api                         # filter by tag

# Scaffold
dlane scaffold <name>
dlane scaffold <name> --force
```

### workspace.yml structure

```yaml
version: 1
default_profile: default

projects:
  - name: gateway
    path: gateway
    gitlab_project: acme/gateway
    strategy: bluegreen
    description: "API Gateway"
    tags: [api, production]

  - name: frontend
    path: frontend
    gitlab_project: acme/frontend
    strategy: plain
    tags: [web]
```

------------------------------------------------------------------------

## Authentication

```bash
dlane login --host https://gitlab.example.com
dlane login --profile staging --host https://gitlab.example.com
dlane logout
dlane whoami
dlane status

dlane profile list
dlane profile use staging
dlane config show
```

Config stored at `~/.config/deploylane/config.toml` with `0600` permissions.

**Non-interactive (CI):**
```bash
export GITLAB_HOST="https://gitlab.example.com"
export GITLAB_TOKEN="glpat-xxxx"
```

------------------------------------------------------------------------

## GitLab project browser

```bash
dlane gitlab list
dlane gitlab list --search gateway
dlane gitlab list --owned
```

------------------------------------------------------------------------

## Security

Add to `.gitignore`:

```
.deploylane/
.deploylane/env/*.local.env
```

- GitLab token stored in `~/.config/deploylane/config.toml` (`0600`)
- Masked variables shown as `<masked>` in plan/diff output
- `.local.env` files are gitignored — safe for local secrets and overrides
- `deploy push` is dry-run by default — requires explicit `--yes`
- `deploy install` requires explicit `--yes` and prompts for sudo password

------------------------------------------------------------------------

## Command reference

### Auth

| Command | Description |
|---------|-------------|
| `dlane login` | Store GitLab token locally |
| `dlane logout` | Remove stored credentials |
| `dlane whoami` | Show current GitLab user |
| `dlane status` | Show active profile and login status |
| `dlane profile list` | List stored profiles |
| `dlane profile use <name>` | Switch active profile |
| `dlane config show` | Show config file path |

### Workspace

| Command | Description |
|---------|-------------|
| `dlane init` | Create `.workspace/workspace.yml` |
| `dlane add <name>` | Add project to workspace |
| `dlane update <name>` | Update project fields |
| `dlane remove <name>` | Remove project from workspace |
| `dlane list` | List workspace projects with status |
| `dlane scaffold <name>` | Regenerate `.deploylane/` files from deploy.yml |

### Variables

| Command | Description |
|---------|-------------|
| `dlane vars get <name>` | Fetch GitLab variables → vars.yml |
| `dlane vars plan <name>` | Preview what would change |
| `dlane vars diff <name>` | Diff local vars.yml vs GitLab |
| `dlane vars apply <name>` | Push vars.yml → GitLab |
| `dlane vars prune <name>` | Delete GitLab vars not in vars.yml |

### Deploy

| Command | Description |
|---------|-------------|
| `dlane deploy push <name>` | Push .env + compose + deploy.sh to server |
| `dlane deploy install <name>` | Push + run install.sh (one-time server setup) |
| `dlane deploy history <name>` | Show deployment history |

### Tools

| Command | Description |
|---------|-------------|
| `dlane gitlab list` | Browse GitLab projects |

------------------------------------------------------------------------

## Development

```bash
pip install -e ".[dev]"
python -m build
```
