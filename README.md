# DeployLane

**Deployment configs as code. GitLab CI variables as code. Reproducible deploys every time.**

DeployLane is a CLI for teams deploying containerized apps to Linux servers with Docker Compose and GitLab CI. It replaces scattered shell scripts and manually managed CI variables with a single, version-controlled workflow.

```bash
pip install deploylane
dlane login --host https://gitlab.example.com
dlane scaffold my-service
dlane deploy push my-service --yes
```

---

## Is this for you?

DeployLane fits your stack if:

- ✅ You deploy Docker Compose apps to Linux servers (VPS, bare metal, cloud VMs)
- ✅ You use GitLab CI to build and push images
- ✅ You manage CI variables manually in the GitLab UI and it's becoming a mess
- ✅ You want zero-downtime blue-green deploys without Kubernetes complexity

It's probably not for you if you're on Kubernetes, AWS ECS, or a fully managed PaaS.

---

## The problem it solves

Most teams start with a `deploy.sh` script that "just works." Over time it becomes:

- Different versions on different servers
- CI variables scattered across projects with no history
- "Works on staging, broken on prod" because someone changed something manually
- New team member spends a day figuring out how deployment actually works

DeployLane makes the entire deployment setup **version-controlled and reproducible**.

---

## How it works

```
Your workstation                  GitLab CI                  Server
────────────────                  ─────────────              ──────
dlane scaffold
  deploy.yml
  vars.yml       → dlane vars apply  → GitLab CI variables ← Pipeline uses these
  compose/                                                        ↓
  nginx/                                                    deploy.sh (CI calls this)
  ci/

                 ← dlane deploy pull ←  docker-compose.yml  (pull before you push)
                                        deploy.sh
                                        .env (runtime state, never overwritten)
                                        nginx.conf

                 → dlane deploy push →  docker-compose.yml  (pull-first protected)
                                        deploy.sh

                 → dlane deploy install → nginx.conf         (one-time setup)
                                          sudoers
                                          install.sh

dlane ci push   → opens MR in GitLab with .gitlab-ci.yml
dlane deploy status  → shows running containers and active color per target
```

1. **`dlane scaffold`** generates all server-side files from a single `deploy.yml`
2. **`dlane vars apply`** pushes your local `vars.yml` to GitLab CI variables
3. **`dlane deploy pull`** fetches the current server state before making changes
4. **`dlane deploy push`** syncs `docker-compose.yml` and `deploy.sh` to the server (blocked if server version differs)
5. **GitLab CI** builds the image and calls `deploy.sh` — that's it

---

## Installation

```bash
pip install deploylane
```

Requires Python 3.10+. Works on macOS and Linux.

---

## Quick start (5 minutes)

### 1. Login

```bash
dlane login --host https://gitlab.example.com
# optionally name your profile and set a registry host
dlane login --profile prod --host https://gitlab.example.com --registry-host registry.example.com
```

### 2. Create a workspace

A workspace tracks multiple services together.

```bash
mkdir ops && cd ops
dlane init
dlane add gateway --gitlab-project acme/gateway --strategy bluegreen
dlane add api     --gitlab-project acme/api     --strategy plain
```

### 3. Scaffold a service

```bash
dlane scaffold gateway
```

This generates `.workspace/gateway/.deploylane/` with:
- `deploy.yml` — targets, ports, strategy (edit this to match your server)
- `vars.yml` — CI variable definitions (edit values, then `vars apply`)
- `compose/`, `nginx/`, `scripts/deploy.sh`, `ci/.gitlab-ci.yml`

### 4. Configure your server details

Edit `.workspace/gateway/.deploylane/deploy.yml`:

```yaml
version: 1
strategy: bluegreen
project: acme/gateway

app:
  name: gateway
  registry_host: registry.acme.com

targets:
  prod:
    host: 10.0.0.1
    user: deploy
    deploy_dir: /home/deploy/gateway
    ports:
      blue: 8080
      green: 8081
```

### 5. Push CI variables

```bash
dlane vars plan gateway     # preview what would change
dlane vars apply gateway    # push to GitLab
```

### 6. Push files to server & install (once per server)

```bash
dlane deploy pull gateway             # fetch server state first (if server already exists)
dlane deploy push gateway --yes       # docker-compose.yml + deploy.sh (pull-first protected)
dlane deploy install gateway --yes    # nginx + sudoers + runs install.sh (one-time)
```

### 7. Add the CI pipeline

```bash
dlane ci push gateway    # opens an MR in acme/gateway with the generated .gitlab-ci.yml
```

Or copy `.workspace/gateway/.deploylane/ci/.gitlab-ci.yml` manually to your repo.
GitLab CI will now build, push, and deploy automatically on every push.

---

## Deployment strategies

| Strategy | How it works | When to use |
|---|---|---|
| `bluegreen` | Two containers, nginx switches traffic — zero downtime | Production |
| `plain` | Single container, `docker compose up -d` | Staging, internal tools |

---

## CI variable management

Stop clicking through the GitLab UI. Manage variables as code:

```bash
dlane vars get gateway       # pull current GitLab variables → vars.yml
dlane vars diff gateway      # see what's different between local and GitLab
dlane vars plan gateway      # preview creates / updates / orphans
dlane vars apply gateway     # push local vars.yml → GitLab
dlane vars prune gateway     # remove GitLab variables not in vars.yml
```

`vars.yml` is version-controlled. Every variable change is a git commit.

**Variable naming convention:**

| Variable | Purpose |
|---|---|
| `{TARGET}_HOST` | Server IP — e.g. `PROD_HOST`, `STAGING_HOST` |
| `{TARGET}_USER` | SSH user |
| `{TARGET}_DEPLOY_DIR` | Deploy directory on server |
| `{TARGET}_SSH_KEY` | Base64-encoded private key |
| `REGISTRY_HOST` | Docker registry hostname |
| `REGISTRY_USER` | Registry login user |
| `REGISTRY_PASS` | Registry login password |

---

## Multi-service workspaces

```bash
dlane list                          # show all services and their status
dlane list --tag api                # filter by tag
dlane deploy push --all --yes       # push all services at once
dlane vars apply --all              # apply variables for all services
```

---

## Local overrides

Keep production secrets out of git with `.local.env` files:

```bash
# .workspace/gateway/.deploylane/env/prod.local.env  (gitignored)
APP_PORT=9090     # override a port without touching the base env
DEBUG=false       # environment-specific toggle
```

Local env is merged on top of the base env before pushing. Use this for machine-specific or temporary overrides — keep secrets and connection strings in GitLab CI variables via `vars.yml`.

---

## Deploy safety

`deploy push` protects against accidental overwrites:

- **compose** — checks server version before pushing. If server differs from local, push is blocked: `run 'dlane deploy pull' first`
- **deploy.sh** — same pull-first check: blocked if server version differs from local
- **.env** — never overwritten if it already exists on the server (contains runtime state like `ACTIVE_COLOR`)
- **`--force`** — bypasses compose and deploy.sh checks when you know what you're doing

```bash
dlane deploy pull gateway                # fetch compose + deploy.sh + env + nginx from all targets
dlane deploy push gateway --yes          # protected push (blocked if server differs)
dlane deploy push gateway --yes --force  # skip version checks
dlane deploy status gateway              # show running containers and active color
```

---

## Workspace profiles

Each workspace can be pinned to a specific profile in `workspace.yml`:

```yaml
default_profile: prod   # always uses this profile, regardless of active global profile
```

All `vars` and `ci` commands use the workspace profile automatically. A warning is shown if it differs from the globally active profile.

---

## CI variable management — masked variables

GitLab never returns masked variable values via the API (by design). This affects `vars get`:

- **Non-masked variables** — full round-trip: `get` → edit → `apply` ✅
- **Masked variables** — write-only: `get` fetches the key and metadata but **not the value**

When you run `vars get`, masked variables with no existing value in `vars.yml` are flagged:
```
⚠ 2 masked variable(s) have no value — fill in vars.yml before running 'vars apply'
  - PROD_SSH_KEY
  - REGISTRY_PASS
```

If `vars.yml` already exists, previously filled values are preserved. `vars apply` skips any masked variable with an empty value rather than pushing an empty string.

**Recommendation:** treat `vars.yml` as the source of truth. Don't rely on `vars get` to recover masked values — fill them in once and keep the file.

---

## Security

- GitLab token stored at `~/.config/deploylane/config.toml` with `0600` permissions
- Masked variables shown as `<masked>` in all output
- `deploy push` requires explicit `--yes` to apply changes
- `deploy push` is **blocked by default** if server state differs from local — run `deploy pull` first
- `deploy install` requires explicit `--yes` and prompts for sudo password interactively
- `.local.env` files are gitignored automatically
- `.env` on the server is **never overwritten** — it holds runtime state (`ACTIVE_COLOR`, etc.)

Add to your `.gitignore`:
```
.workspace/
```

---

## Command reference

### Auth
| Command | Description |
|---|---|
| `dlane login` | Store credentials locally and verify against the API |
| `dlane logout` | Remove stored credentials |
| `dlane whoami` | Show current user for the active profile |
| `dlane status` | Show active profile and login status |
| `dlane profile list` | List stored profiles |
| `dlane profile use <name>` | Switch active profile |

### Workspace
| Command | Description |
|---|---|
| `dlane init` | Create workspace in current directory |
| `dlane add <name>` | Add a service to the workspace |
| `dlane update <name>` | Update service fields |
| `dlane remove <name>` | Remove a service |
| `dlane list` | List services with status |
| `dlane scaffold <name>` | Generate / refresh deployment files |

### Variables
| Command | Description |
|---|---|
| `dlane vars get <name>` | Fetch GitLab variables → `vars.yml` |
| `dlane vars plan <name>` | Preview what would change |
| `dlane vars diff <name>` | Diff local vs GitLab |
| `dlane vars apply <name>` | Push `vars.yml` → GitLab |
| `dlane vars prune <name>` | Delete GitLab vars not in `vars.yml` |

### Deploy
| Command | Description |
|---|---|
| `dlane deploy diff <name>` | Show what would change on the server without pushing |
| `dlane deploy pull <name>` | Fetch compose, deploy.sh, env, nginx from server (all targets) |
| `dlane deploy push <name>` | Push compose + `deploy.sh` to server (pull-first protected, never overwrites `.env`) |
| `dlane deploy install <name>` | One-time server setup (nginx + sudoers + install.sh) |
| `dlane deploy status <name>` | Show running containers and active color per target |
| `dlane deploy history <name>` | Show deployment history |

### CI
| Command | Description |
|---|---|
| `dlane ci lint <name>` | Validate local `.gitlab-ci.yml` via GitLab lint API |
| `dlane ci pull <name>` | Fetch `.gitlab-ci.yml` from GitLab repo → local |
| `dlane ci push <name>` | Lint + push local `.gitlab-ci.yml` to GitLab via MR |

### Tools
| Command | Description |
|---|---|
| `dlane --version` | Show installed version |
| `dlane gitlab list` | Browse GitLab projects |

---

## Platform support

| Platform | vars | ci | deploy |
|---|---|---|---|
| GitLab | ✅ Full | ✅ Full (MR flow) | ✅ Full |
| GitHub Actions | 🧪 Experimental | ❌ Not yet | ✅ Full (SSH-based) |

GitHub Actions variable/secret management is available via `pip install deploylane[github]` but is not yet covered by the full command set (`ci pull/push` is GitLab-only for now).

---

## Development

```bash
git clone https://github.com/ahmetatakan/deploylane
cd deploylane
pip install -e ".[dev]"
python -m build
```
