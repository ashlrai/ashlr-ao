# Ashlr AO — Agent Orchestrator

## What This Is

Ashlr is a **local-first agent orchestration platform**. One developer, many AI coding agents (Claude Code, Codex, etc.), multiple repos, single command center.

**Current state**: Fully functional. Server (~10.8K lines) + dashboard (~9800 lines) + ~1240 tests. All 5 development phases + multi-user auth + deployment infra + production hardening + open-core licensing complete. Installable via `pip install ashlr-ao`. Ready for multi-user deployment.

## Architecture

**Two files. That's the entire application.** Packaged as `ashlr_ao` (pip-installable).

- `ashlr_ao/server.py` — Python aiohttp server. Manages agents via tmux, serves dashboard, REST + WebSocket APIs, SQLite persistence, system metrics, LLM intelligence.
- `ashlr_ao/dashboard.html` — Single HTML file served at `/`. All CSS + JS inline. No build step.
- `ashlr_ao/logo.png` — Logo served at `/logo.png`.
- `ashlr_ao/__init__.py` — Package init with `__version__` (single source of truth).
- `ashlr_ao/__main__.py` — Enables `python -m ashlr_ao`.

### Supporting files

- `pyproject.toml` — Package metadata, dependencies, `ashlr` CLI entry point
- `ashlr_server.py` — Backward-compat shim (re-exports from `ashlr_ao.server` so tests and old imports still work)
- `generate_license.py` — Standalone Ed25519 keypair + JWT license key generator (not part of package)
- `requirements.txt` — Reference copy of dependencies (canonical source is pyproject.toml)
- `start.sh` — Launch script (creates venv, `pip install -e .`, checks tmux/backends, starts server)
- `~/.ashlr/ashlr.yaml` — User config (auto-created on first run)
- `~/.ashlr/ashlr.db` — SQLite database (agent history, projects, workflows)

## Quick Start

```bash
# Install from pip
pip install ashlr-ao
ashlr

# Or run from source
./start.sh

# Optional: enable LLM-powered summaries + NLU command parsing
export XAI_API_KEY="your-key"
ashlr
```

Dashboard opens at `http://127.0.0.1:5111`. Override port with `ASHLR_PORT=8080`.

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Server | Python 3.11+ / aiohttp | Async HTTP + WebSocket. Process management via psutil. |
| Frontend | Vanilla JS (ES2022+) | Zero dependencies. No build step. |
| Persistence | SQLite via aiosqlite | Zero-config. Single file. Agent history, projects, workflows. |
| Process mgmt | tmux | Session isolation, output capture, proven reliability. |
| Licensing | PyJWT + Ed25519 | Offline-first signed JWT. No phone-home. |
| Intelligence | xAI Grok (optional) | OpenAI-compatible API. Summaries, NLU parsing, fleet analysis. |
| Voice | Web Speech API | Browser-native push-to-talk. Zero dependencies. |
| Fonts | Instrument Sans + JetBrains Mono | Display + monospace. |

## How Agents Work

Each agent is a tmux session running a CLI tool:

1. **Spawn**: `tmux new-session -d -s ashlr-{id} -x 200 -y 50 -c {working_dir}` → sends CLI command
2. **Capture**: `tmux capture-pane` every 1s → parse status, detect questions, extract summary
3. **Interact**: `tmux send-keys` → sends user responses to agent's stdin
4. **Kill**: `/exit` → wait 3s → `tmux kill-session`
5. **Status**: Regex pattern matching on terminal output (planning/working/waiting/error/idle)

### Claude Code specifics
- Default args: `--dangerously-skip-permissions`
- Plan mode: replaces with `--permission-mode plan`
- Task passed as positional argument after TUI ready detection
- Model selection via `--model` flag
- Tool restriction via `--allowedTools` flag

## REST API

```
# Agents
GET    /api/agents              List all agents
POST   /api/agents              Spawn agent (role, task, working_dir, backend, plan_mode, project_id, model, tools)
GET    /api/agents/{id}         Agent details + output (includes git_branch)
GET    /api/agents?branch=x     Filter agents by git branch (also ?project_id=, ?status=)
DELETE /api/agents/{id}         Kill agent
POST   /api/agents/{id}/send    Send message to agent (max 50K chars)
POST   /api/agents/{id}/pause   Pause (SIGTSTP)
POST   /api/agents/{id}/resume  Resume (SIGCONT)
POST   /api/agents/{id}/restart Restart with same config
POST   /api/agents/bulk         Bulk action (pause/resume/kill/restart) on multiple agents

# Projects
GET    /api/projects            List projects
POST   /api/projects            Add project (name, path — must be real directory under ~/ or /tmp)
PUT    /api/projects/{id}       Update project (name, path, description)
DELETE /api/projects/{id}       Delete project

# Workflows
GET    /api/workflows           List workflows
POST   /api/workflows           Create workflow (name, agent specs with depends_on DAG)
PUT    /api/workflows/{id}      Update workflow (validates circular deps)
POST   /api/workflows/{id}/run  Execute workflow
DELETE /api/workflows/{id}      Delete workflow

# Intelligence (requires XAI_API_KEY)
POST   /api/intelligence/command    NLU command parsing (natural language → intent)
GET    /api/intelligence/insights   Fleet analysis insights

# Auth (session-based, cookie auth)
GET    /api/auth/status         Check if auth required + current user
POST   /api/auth/register       Register (first user = admin + creates org)
POST   /api/auth/login          Login (email/password) → sets session cookie
POST   /api/auth/logout         Logout (clears session)
GET    /api/auth/me             Current user info
POST   /api/auth/invite         Admin-only: invite user with temp password (Pro)
GET    /api/auth/team           List org members (Pro)

# Licensing
GET    /api/license/status      Current license info + effective limits (public)
POST   /api/license/activate    Activate license key (admin-only)
DELETE /api/license/deactivate  Deactivate license, revert to Community (admin-only)

# System
GET    /api/system              System metrics (CPU, RAM, agents)
GET    /api/health              Health check
GET    /api/config              Current config
PUT    /api/config              Update config (validates, saves to YAML + runtime)
GET    /api/roles               Available roles
GET    /api/backends            Available backends with capabilities
GET    /api/costs               Cost estimates (active + historical)
POST   /api/agents/{id}/summarize   Trigger LLM summary
```

## WebSocket Protocol (`/ws`)

### Server → Client
```json
{"type": "agent_update", "agent": {...}}
{"type": "agent_output", "agent_id": "a7f3", "lines": [...]}
{"type": "metrics", "cpu_pct": 34.2, "memory": {...}, "agents_active": 5}
{"type": "event", "event": "agent_needs_input", "agent_id": "a7f3", "message": "..."}
{"type": "sync", "agents": [...], "projects": [...], "config": {...}, "license": {...}, "effective_max_agents": 5}
{"type": "license_update", "license": {...}, "effective_max_agents": 100}
{"type": "intelligence_alert", "insight": {...}}
{"type": "file_conflict", "conflict": {...}}
```

### Client → Server
```json
{"type": "spawn", "role": "backend", "name": "auth-api", "working_dir": "/path", "task": "...", "plan_mode": true}
{"type": "send", "agent_id": "a7f3", "message": "yes, proceed"}
{"type": "kill", "agent_id": "a7f3"}
{"type": "pause", "agent_id": "a7f3"}
{"type": "resume", "agent_id": "a7f3"}
```

## Dashboard Features

### Agent Card Grid
- Cards show: role icon, name, project, git branch badge, live summary, context bar, status badge
- Status-based styling: working (role color pulse), waiting (orange attention), error (red), planning (yellow)
- Click card → deep view. Click waiting card → inline interaction sheet.
- Filter by: project dropdown, branch dropdown, status chips, text search
- Dynamic grid layout adapts to agent count

### Spawn Dialog
- Resume Previous Session section — shows resumable sessions from history, click to pre-fill form
- 9 built-in roles with icons
- Project selector, working dir with autocomplete, backend selector
- Plan mode toggle, model selector, tools restriction
- Quick templates (Code Review, Tests, Bug Fix, Feature, Security, Refactor)
- Fleet presets (Full Stack, Review Team, Quality Check) — spawn 2-3 agents at once
- Recent tasks carousel from localStorage

### Interaction (Agent Needs Input)
- Attention queue: Cmd+Shift+A — lists all waiting/errored agents
- Approve/Reject/Custom response buttons
- Plan mode: "Approve Plan" / "Revise" / "View Plan"
- Keyboard: A/Y = approve, R/N = reject

### Deep View
- Full terminal output with ANSI colors, auto-scroll, search (Cmd+F)
- Activity tab: parsed events (file reads/edits, bash commands, test results, errors, git ops)
- Scratchpad tab: per-agent notes (persisted to SQLite)
- Line numbers, timestamps, code block folding

### Bulk Operations
- Cmd+Shift+S toggles select mode, Cmd+A selects all visible
- Bulk pause/resume/restart/kill with confirmation
- Batch send with variable templates ({name}, {role}, {task})

### Command Bar
- `@agent-name` mention autocomplete
- `/command` autocomplete (spawn, kill, pause, resume, status, help)
- Natural language routing via LLM (if XAI_API_KEY set)
- Smart single-agent auto-targeting

### Keyboard Shortcuts
| Key | Action |
|-----|--------|
| Cmd+K | Command palette |
| Cmd+N | New agent |
| Cmd+, | Settings |
| Cmd+Shift+S | Toggle bulk select |
| Cmd+Shift+A | Attention queue (waiting agents) |
| 1-9 | Focus agent / select role |
| Escape | Close overlay / back to grid |
| Space (hold) | Push-to-talk |

### Additional
- Dynamic favicon (color shifts by fleet status)
- Ambient background gradient (fleet health indicator)
- Audio feedback (Web Audio API tones)
- Settings panel: max agents, backends, thresholds, cost budgets, alert patterns
- License settings: plan badge, key activation/deactivation, tier details, expiry info
- Import/export config and fleet state

## Intelligence Layer (Optional — requires XAI_API_KEY)

Single `IntelligenceClient` class using xAI Grok via OpenAI-compatible API:
- **Summaries**: 1-line agent status summaries from terminal output
- **NLU**: Natural language command parsing ("spawn 3 agents on auth-service")
- **Fleet analysis**: Meta-agent detects conflicts, stuck agents, handoff opportunities (runs every 30s)
- Circuit breaker: 5 failures → 60s cooldown
- Falls back to regex parsing when LLM unavailable

## Agent Status Detection

Terminal output is pattern-matched every capture cycle:
- **planning**: plan mode indicators, thinking patterns
- **working**: file operations, tool usage, progress bars
- **waiting**: questions, approval prompts, input requests
- **error**: exceptions, tracebacks, failures
- **idle/complete**: done indicators, no recent output

Enhanced for Claude Code: `╭─`, `╰─`, `Press Enter to retry`, `Type your response`, `Thinking...`

## Built-in Roles

| Key | Color | Name |
|-----|-------|------|
| `frontend` | `#8B5CF6` | Frontend Engineer |
| `backend` | `#3B82F6` | Backend Engineer |
| `devops` | `#F97316` | DevOps Engineer |
| `tester` | `#22C55E` | QA / Tester |
| `reviewer` | `#EAB308` | Code Reviewer |
| `security` | `#EF4444` | Security Auditor |
| `architect` | `#06B6D4` | Architect |
| `docs` | `#A855F7` | Documentation |
| `general` | `#64748B` | General Purpose |

All roles use Lucide SVG icons (53 icons in ICONS object). No emoji in production UI.

## Config (`~/.ashlr/ashlr.yaml`)

```yaml
server:
  host: "127.0.0.1"
  port: 5111
  log_level: "INFO"
agents:
  max_concurrent: 16
  default_role: "general"
  default_working_dir: "~/Projects"
  output_capture_interval_sec: 1.0
  memory_limit_mb: 2048
  default_backend: "claude-code"
  backends:
    claude-code:
      command: "claude"
      args: ["--dangerously-skip-permissions"]
    codex:
      command: "codex"
      args: []
llm:
  enabled: false
  provider: "xai"
  model: "grok-4-1-fast-reasoning"
  api_key_env: "XAI_API_KEY"
  base_url: "https://api.x.ai/v1"
  summary_interval_sec: 10.0
  meta_interval_sec: 30.0
voice:
  enabled: true
  ptt_key: "Space"
licensing:
  key: ""                      # JWT license key (set via API or YAML)
display:
  theme: "dark"
  cards_per_row: 4
```

## Environment Variables

| Var | Required | Purpose |
|-----|----------|---------|
| `XAI_API_KEY` | No | All intelligence features (summaries, NLU, fleet analysis) via xAI Grok |
| `ASHLR_PORT` | No | Override port (default 5111) |
| `ASHLR_HOST` | No | Override bind host (default 127.0.0.1, use 0.0.0.0 for Docker) |
| `ASHLR_ALLOWED_ORIGINS` | No | CORS allowed origin (default `*`, set to domain for production) |
| `CLAUDECODE` | No | Force demo mode (dev/testing without real CLI) |

## Background Tasks (6, supervised with auto-restart)

1. **Output capture** (1s) — tmux pane capture, status detection, summary generation
2. **Metrics** (2s) — CPU, memory, per-agent resource tracking
3. **Health check** (5s) — stall detection, hung agent detection, memory pressure
4. **Memory watchdog** (10s) — per-agent memory limits, system pressure response
5. **Meta-agent** (30s, optional) — fleet analysis via LLM, conflict detection
6. **Archive cleanup** (1hr) — purges archived agent records older than 48 hours

## Coding Standards

- Python 3.11+ with type hints
- Dataclasses for all models
- async/await throughout
- Graceful shutdown: clean all tmux sessions on SIGINT/SIGTERM
- Startup: clean orphaned tmux sessions from prior crashes
- NEVER crash — try/except with meaningful error handling
- All dict iterations use `list()` snapshots (prevent RuntimeError during async)
- Security: working_dir restricted to home/tmp, message size limits, rate limiting, CSP headers, request size limits, ownership enforcement on all mutation endpoints
- ~1240 pytest tests across 12 test files

## Multi-User Auth

Session-based authentication with bcrypt password hashing:
- First user to register becomes admin and creates the organization
- Subsequent users must be invited by admin (temp password generated)
- Session cookies with HttpOnly, SameSite=Strict, Secure (behind HTTPS)
- Agent ownership: spawned agents tagged with owner, only owner or admin can control
- All org members can view all agents (command center purpose)
- Bearer token fallback for API/CLI access (backward compat)

## Licensing (Open Core)

Offline-first Ed25519-signed JWT licensing. No phone-home, no license server. Public key embedded in `server.py`, private key held by issuer.

### Tiers

| | Community (free) | Pro (paid) |
|---|---|---|
| Concurrent agents | 5 | Up to 100 |
| Users/seats | 1 | Up to 50 |
| Intelligence (LLM) | Gated | Included |
| Workflows | Gated | Included |
| Fleet presets | Gated | Included |
| Multi-user auth | Gated | Included |
| Core orchestration | Full | Full |

### How It Works

- `License` dataclass: tier, max_agents, max_seats, features, expiry
- `COMMUNITY_LICENSE` singleton: default when no key or key is invalid/expired
- `validate_license(key)`: decode JWT, verify Ed25519 signature, return License or COMMUNITY_LICENSE (never crash)
- `_effective_max_agents(app)`: `min(config.max_agents, license.max_agents)` — enforced at all 6 spawn points
- `_check_feature(request, feature)`: returns 403 JSON with `error`, `feature`, `current_plan` or None

### Feature Gating

Gated features return 403 with `{"error": "...", "feature": "...", "current_plan": "community"}`:
- `intelligence` → intelligence_command, get_intelligence_insights
- `workflows` → create_workflow, update_workflow, run_workflow
- `fleet_presets` → batch_spawn
- `multi_user` → auth_invite, auth_team

### Dashboard Integration

- Plan badge in header: "Community" (gray) or "Pro" (purple gradient)
- License settings section: key input, activate/deactivate buttons, plan details
- 403 responses show upgrade toast with feature name
- Fleet presets show "Pro required" toast on Community tier
- Max agents input clamped to license ceiling with note

### License Key Generation

```bash
# Generate Ed25519 keypair
python generate_license.py generate-keypair --output-dir ./keys

# Sign a Pro license (365 days, 100 agents)
python generate_license.py generate --private-key ./keys/license_private.pem --org-id my-org --tier pro --max-agents 100 --days 365

# Decode a license key (debug, no verification)
python generate_license.py decode <jwt-token>
```

### Admin-Only Config

When auth is enabled, `PUT /api/config` requires admin role. `max_agents` in config updates is clamped to the license ceiling.

## Deployment

```bash
# Local — pip install
pip install ashlr-ao && ashlr

# Local — from source
./start.sh

# Docker + HTTPS
ASHLR_DOMAIN=ashlr.yourdomain.com docker compose up -d
```

Files: `Dockerfile`, `docker-compose.yml`, `Caddyfile`
- Caddy auto-provisions Let's Encrypt HTTPS certificates
- SQLite data persisted via Docker volume
- Set `ASHLR_ALLOWED_ORIGINS` to your domain for CORS in production

## Known Limitations

1. **File conflict detection only** — warns when two agents edit the same file, but doesn't prevent it. Watch the activity feed.
2. **Cost tracking is estimated** — heuristic based on character count, not real API metering.
3. **Working dir restricted to ~/\* and /tmp** — security measure. Use symlinks if repos are elsewhere.
