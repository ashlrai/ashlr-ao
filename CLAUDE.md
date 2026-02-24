# Ashlar AO — Agent Orchestrator

## What This Is

Ashlar is a **local-first agent orchestration platform**. It lets one developer manage many AI coding agents (Claude Code, Codex, etc.) across multiple projects from a single command center.

It is NOT a dashboard. It is NOT a terminal multiplexer. It is an **AI-native control surface** for the agentic engineering era — where every agent is visible at a glance, voice is a first-class input, and the management layer itself is intelligent.

## Architecture

**Two files. That's the entire application.**

- `ashlar_server.py` — Python aiohttp server (~1500-2000 lines). Manages agents via tmux, serves the dashboard, provides REST + WebSocket APIs, collects system metrics, handles configuration.
- `ashlar_dashboard.html` — Single HTML file (~2000-2500 lines). Served by the Python server at `/`. Contains all CSS + JS inline. No build step, no bundler, no node_modules.

Why monolithic? Because YOU (Claude Code) can read the entire system in one shot, understand it fully, and make confident changes. No import chains to trace. Change → restart server (1 second) → refresh browser → see result. We split into multiple files later when complexity demands it.

### Supporting files

- `requirements.txt` — 4 Python packages (aiohttp, aiohttp-cors, psutil, pyyaml)
- `ashlar.yaml` — User configuration (auto-created on first run at ~/.ashlar/ashlar.yaml)
- `start.sh` — Launch script (installs deps, checks tmux, starts server)

## Tech Stack

| Choice | Why |
|--------|-----|
| **Python 3.11+** | Best for process management (subprocess, psutil, tmux). Zero build step. Runs on Mac and Linux. |
| **aiohttp** | Async HTTP + WebSocket in one library. No framework overhead. |
| **Single HTML file** | No build step. Fastest iteration with Claude Code. |
| **Vanilla JS (ES2022+)** | No React, no framework. Zero dependencies = zero breakage. |
| **SQLite** | Zero-config persistence. Single file. Perfect for local-first. |
| **tmux** | Proven terminal multiplexer. Process isolation, output capture, session persistence. |
| **Web Speech API** | Browser-native push-to-talk. Zero dependencies. |
| **WebSocket** | Real-time bidirectional. Native browser support. |

## How Agents Work

Each "agent" is a tmux session running a CLI tool (claude, codex, etc.):

1. **Spawn**: `tmux new-session -d -s ashlar-{id} -x 200 -y 50` then send the claude command
2. **Capture output**: `tmux capture-pane -t ashlar-{id} -p -S -100` every ~1 second
3. **Send commands**: `tmux send-keys -t ashlar-{id} '{message}' Enter`
4. **Kill**: Send `/exit` first, wait 3s, then `tmux kill-session -t ashlar-{id}`
5. **Status detection**: Parse terminal output for patterns (planning, working, waiting for input, error, complete)

## Data Models

### Agent
```python
@dataclass
class Agent:
    id: str                    # Short hex like "a7f3"
    name: str                  # User-friendly, e.g., "auth-api"
    role: str                  # Role key, e.g., "backend"
    status: str                # spawning|planning|working|waiting|idle|error|paused
    project_id: str | None     # Which project this agent belongs to
    working_dir: str           # Absolute path to working directory
    backend: str               # "claude-code" | "codex" | "custom"
    task: str                  # Current task description
    summary: str               # 1-2 line live summary of what agent is doing
    context_pct: float         # Context window usage 0.0-1.0
    memory_mb: float           # RSS memory usage
    needs_input: bool          # True if waiting for user response
    input_prompt: str | None   # What the agent is asking
    output_lines: list[str]    # Ring buffer of last N terminal output lines
    tmux_session: str          # tmux session name
    pid: int | None            # OS process ID
    created_at: datetime
    updated_at: datetime
```

### Agent Status Colors (for card UI)
- **SPAWNING**: Skeleton loading animation
- **PLANNING**: Yellow/amber left border, thinking indicator
- **WORKING**: Role color left border, subtle pulse animation
- **WAITING**: Orange left border, attention pulse (agent needs user input)
- **IDLE**: Gray left border, dimmed
- **ERROR**: Red left border, error icon
- **PAUSED**: Entire card dimmed, pause overlay

### Roles (built-in)
| Key | Icon | Color | Name |
|-----|------|-------|------|
| `frontend` | 🎨 | `#8B5CF6` | Frontend Engineer |
| `backend` | ⚙ | `#3B82F6` | Backend Engineer |
| `devops` | 🚀 | `#F97316` | DevOps Engineer |
| `tester` | 🧪 | `#22C55E` | QA / Tester |
| `reviewer` | 👁 | `#EAB308` | Code Reviewer |
| `security` | 🔒 | `#EF4444` | Security Auditor |
| `architect` | 🏗 | `#06B6D4` | Architect |
| `docs` | 📝 | `#A855F7` | Documentation |
| `general` | 🤖 | `#64748B` | General Purpose |

## WebSocket Protocol

### Server → Client
```json
{"type": "agent_update", "agent": {...}}
{"type": "agent_output", "agent_id": "a7f3", "lines": [...]}
{"type": "metrics", "cpu_pct": 34.2, "memory": {...}, "agents_active": 5}
{"type": "event", "event": "agent_needs_input", "agent_id": "a7f3", "message": "..."}
{"type": "sync", "agents": [...], "projects": [...], "config": {...}}
```

### Client → Server
```json
{"type": "spawn", "role": "backend", "name": "auth-api", "working_dir": "/path", "task": "...", "plan_mode": true}
{"type": "send", "agent_id": "a7f3", "message": "yes, proceed"}
{"type": "kill", "agent_id": "a7f3"}
{"type": "pause", "agent_id": "a7f3"}
{"type": "resume", "agent_id": "a7f3"}
{"type": "sync_request"}
```

## REST API
```
GET    /api/agents              List all agents
POST   /api/agents              Spawn new agent
GET    /api/agents/{id}         Agent details + output
DELETE /api/agents/{id}         Kill agent
POST   /api/agents/{id}/send    Send message to agent
POST   /api/agents/{id}/pause   Pause agent
POST   /api/agents/{id}/resume  Resume agent
GET    /api/system              System metrics
GET    /api/roles               Available roles
GET    /api/projects            List projects
POST   /api/projects            Add project
GET    /api/config              Get config
PUT    /api/config              Update config
```

## UI Design

### Main View: Agent Card Grid
```
┌─────────────────────────────────────────────────────────────────┐
│  ▣ ASHLAR   Projects ▾   [+ Agent]         CPU 34%  RAM 62%    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ 🎨 auth  │ │ ⚙ api    │ │ 🔒 audit │ │ 🧪 tests │           │
│  │ WORKING  │ │ WAITING  │ │ COMPLETE │ │ PLANNING │           │
│  │ Writing  │ │ Approve? │ │ 3 issues │ │ Analyzi  │           │
│  │ ████░░░░ │ │ ██░░░░░░ │ │ ████████ │ │ █░░░░░░░ │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  🎤 PTT Ready  │  > Type a command or message...         [Send] │
└─────────────────────────────────────────────────────────────────┘
```

### Each Agent Card Shows:
- Role icon + agent name + overflow menu
- Project name (dimmed)
- 1-2 line live summary of what agent is doing
- Context window usage bar (percentage)
- Status badge + time since last update

### Inline Interaction (Critical Feature)
When an agent's status is WAITING:
1. Card shows truncated version of what agent is asking
2. Click card → expands inline or opens slide-out panel
3. See agent's question/plan in full
4. Action buttons: Approve, Reject, Edit, or type custom response
5. Response sent to agent's tmux session
6. Card collapses, agent resumes

### Deep View (Click Into Agent)
Full terminal output with ANSI color rendering, auto-scroll, message input at bottom. Back button returns to grid.

### Command Palette
`Cmd+K` or `/` opens a quick-action palette for spawning, killing, pausing agents.

### Keyboard Shortcuts
| Key | Action |
|-----|--------|
| `Cmd+K` or `/` | Command palette |
| `Cmd+N` | New agent |
| `1-9` | Focus agent by position |
| `Escape` | Back to grid |
| `Space` (hold) | Push-to-talk |
| `Cmd+Enter` | Send to focused agent |
| `Cmd+Shift+A` | Approve waiting agent |

## Voice System
- **Push-to-talk**: Hold Space, speak, release
- **Web Speech API**: Browser-native, zero dependencies
- **Intent parsing**: Regex + keyword matching for MVP
  - "spawn a backend agent on payment-service" → spawn action
  - "approve agent 2" → approve action
  - "what's agent 3 doing" → status query
- **Audio feedback**: Web Audio API tones (PTT click, confirmation, error)

## Agent Output Status Detection
Parse terminal output to determine agent status:
```python
STATUS_PATTERNS = {
    "planning": [r"(?i)plan:", r"(?i)let me (think|analyze|plan)"],
    "working": [r"(?i)(writing|creating|reading|editing) .+\.\w+", r"█+░*"],
    "waiting": [r"(?i)(do you want|shall I|should I)", r"> $"],
    "error": [r"(?i)(error|exception|traceback|failed)"],
    "complete": [r"(?i)(done|complete|finished|successfully)"],
}
```

## Development Phases

### Phase 1: Foundation (START HERE)
Get a working server + dashboard that can spawn agents and show live cards.
- ashlar_server.py with agent management via tmux
- ashlar_dashboard.html with card grid + WebSocket updates
- REST API for spawn, kill, list
- Terminal output capture + display
- System metrics bar

### Phase 2: Interaction
Inline interaction, status detection, context tracking, notifications.

### Phase 3: Voice + Commands
Push-to-talk, command palette, keyboard shortcuts, audio feedback.

### Phase 4: Projects + Persistence
Project registry, SQLite, agent history, workflow templates, settings.

### Phase 5: Intelligence
LLM-powered summaries, smart notifications, agent coordination, multi-backend support.

## Coding Standards
- Python 3.11+ with full type hints
- Dataclasses for all data models
- async/await throughout (no blocking calls on the event loop)
- Graceful shutdown: clean up all tmux sessions on SIGINT/SIGTERM
- Colored logging (console) + structured logging (file)
- Works on macOS AND Linux
- Auto-creates ~/.ashlar/ config directory on first run
- NEVER crash — wrap everything in try/except with meaningful error handling

## Key Implementation Notes
- Use `shutil.which('tmux')` to verify tmux is installed at startup
- Use `shutil.which('claude')` to verify claude CLI, with helpful error message if not found
- Agent IDs are 4 hex chars: `uuid.uuid4().hex[:4]`
- Support a "demo mode" that spawns bash sessions with simulated behavior (for development/testing without Claude CLI)
- The dashboard HTML is served from disk by the Python server at `/`. It is NOT embedded as a string — it's loaded from `ashlar_dashboard.html` in the same directory.
- All real-time updates go through WebSocket. REST is for one-off requests.
- tmux is non-negotiable for MVP. Don't reinvent process management.

## Config (ashlar.yaml, auto-created at ~/.ashlar/ashlar.yaml)
```yaml
server:
  host: "127.0.0.1"
  port: 5000
  log_level: "INFO"
agents:
  max_concurrent: 16
  default_role: "general"
  default_working_dir: "~/Projects"
  output_capture_interval_sec: 1.0
  memory_limit_mb: 2048
  backends:
    claude-code:
      command: "claude"
      args: ["--dangerously-skip-permissions"]
    codex:
      command: "codex"
      args: []
  default_backend: "claude-code"
voice:
  enabled: true
  ptt_key: "Space"
  feedback_sounds: true
display:
  theme: "dark"
  cards_per_row: 4
```

## What Success Looks Like

You open Ashlar. Agent cards load. You hold Space: "Start three agents on payment-service — API, tests, and security audit. Plan mode." Three cards appear, turn yellow. You check email for 10 minutes. Come back — two cards are orange (plans ready), one is red (found a bug). You click, approve, fix, and manage 4 agents without ever opening a terminal. Flow state preserved.
