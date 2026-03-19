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
dlane scaffold                                           .env
  deploy.yml     → dlane deploy push →                  docker-compose.yml
  vars.yml                                               deploy.sh
  compose/       → dlane deploy install →               nginx.conf
  nginx/                                                 install.sh (run once)

dlane vars apply → GitLab CI variables ← Pipeline uses these
                                              ↓
                                          deploy.sh (zero-downtime or restart)
```

1. **`dlane scaffold`** generates all server-side files from a single `deploy.yml`
2. **`dlane vars apply`** pushes your local `vars.yml` to GitLab CI variables
3. **`dlane deploy push`** syncs configs to the server
4. **GitLab CI** builds the image and calls `deploy.sh` — that's it

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
dlane deploy push gateway --yes       # .env + docker-compose.yml + deploy.sh
dlane deploy install gateway --yes    # nginx + sudoers + runs install.sh
```

### 7. Add the CI pipeline

Copy `.workspace/gateway/.deploylane/ci/.gitlab-ci.yml` to your `acme/gateway` repo.
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
DATABASE_URL=postgres://prod-db:5432/myapp
STRIPE_SECRET=sk_live_...
```

Local env is merged on top of the base env before pushing. Safe for secrets.

---

## Security

- GitLab token stored at `~/.config/deploylane/config.toml` with `0600` permissions
- Masked variables shown as `<masked>` in all output
- `deploy push` is **dry-run by default** — requires explicit `--yes`
- `deploy install` requires explicit `--yes` and prompts for sudo password interactively
- `.local.env` files are gitignored automatically

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
| `dlane deploy push <name>` | Push `.env` + compose + `deploy.sh` to server |
| `dlane deploy install <name>` | One-time server setup (nginx + sudoers + install.sh) |
| `dlane deploy history <name>` | Show deployment history |

### Tools
| Command | Description |
|---|---|
| `dlane gitlab list` | Browse GitLab projects |

---

## Development

```bash
git clone https://github.com/yourorg/deploylane
cd deploylane
pip install -e ".[dev]"
python -m build
```
