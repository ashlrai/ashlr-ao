# ASHLR — Architecture & Development Bible

**Version**: 0.1.0 | **Last Updated**: February 2026
**Status**: Pre-development — ready for Claude Code implementation

---

## 1. WHAT IS ASHLR?

Ashlr is a **local-first agent orchestration platform** that lets one developer manage many AI coding agents across multiple projects from a single command center.

It is not a dashboard. It is not a terminal multiplexer. It is an **AI-native control surface** for the agentic engineering era — where the management layer itself is intelligent, the primary input is voice, and every agent is visible at a glance.

### The Problem

Today's AI coding tools (Claude Code, Codex, Cursor, etc.) are powerful but isolated. Each runs in its own terminal or window. When you're running 5-10+ agents across multiple projects:

- **Context switching kills flow**: Jumping between terminals to check agent status, losing track of who's doing what
- **No signal when agents need you**: Agents get stuck or go down wrong paths and you don't notice until you manually check
- **No coordination between agents**: When Agent A finishes an API, you have to manually tell Agent B about it
- **No overview**: There's no single place to see "here's everything that's happening across all my projects right now"

### The Solution

Ashlr gives you a **command center** where every agent is a card showing real-time status. You see at a glance what's working, what's stuck, what needs your attention. You speak commands ("spawn a backend agent for the auth feature"), respond to agent questions inline, and click into any agent for the full terminal view. Ashlr itself is intelligent — it monitors your agents, detects when they need help, and coordinates information between them.

### Who Is This For?

Agentic engineers — developers who use AI coding agents as their primary development workflow. People who already run multiple Claude Code sessions or Codex instances and want to scale up without losing control.

---

## 2. PRODUCT VISION

### Core Principles

1. **Agent-first, AI-native**: This is not a traditional dev tool with AI bolted on. Every interaction is designed around managing AI agents.

2. **Voice as a first-class input**: You can't type in 10 terminals at once, but you CAN talk. Push-to-talk with a keyboard shortcut for zero-friction commands.

3. **Visibility over control**: The most important thing is knowing what's happening. Control flows from visibility.

4. **Build for 6-months-from-now models**: Agents will get faster, cheaper, more autonomous. Build for a world where you're running 20-50 agents, not 3-5.

5. **Flow state preservation**: Every design decision should ask: "does this keep the developer in flow state or break it?"

6. **Works today, scales tomorrow**: Launch as a management layer for Claude Code sessions. Evolve toward a full agent runtime that can use any model provider.

### The 30-Second Experience

1. You launch Ashlr. Your projects and any persistent agents load.
2. You see a grid of agent cards — each color-coded by status.
3. You hold your PTT key: "Spin up three agents on the payment service — one for the API, one for tests, one for security audit. Start them all in plan mode."
4. Three cards appear. They turn yellow (thinking), then blue (planning). You see 1-2 line summaries updating in real-time.
5. Agent 1 finishes its plan. Its card pulses — it needs your approval. You click it, see the plan inline, hit Approve.
6. Agent 3 finds a critical vulnerability. Its card turns red. A notification appears. You click in, review, and say "fix it."
7. Through all of this, you never left the command center. You never opened a terminal. You never lost context.

---

## 3. ARCHITECTURE

### High-Level Overview

```
┌─────────────────────────────────────────────────────┐
│                    ASHLR                             │
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │              Web Dashboard (HTML/JS)             │ │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐           │ │
│  │  │Agent    │ │Terminal │ │Metrics  │ Voice Bar  │ │
│  │  │Cards    │ │View     │ │Panel    │ Cmd Input  │ │
│  │  └────┬────┘ └────┬────┘ └────┬────┘            │ │
│  │       └────────────┼──────────┘                  │ │
│  │                    │ WebSocket                    │ │
│  └────────────────────┼─────────────────────────────┘ │
│                       │                               │
│  ┌────────────────────┼─────────────────────────────┐ │
│  │         Ashlr Core (Python/aiohttp)             │ │
│  │                    │                              │ │
│  │  ┌────────────┐ ┌─┴──────────┐ ┌─────────────┐  │ │
│  │  │  Agent     │ │  WebSocket │ │  REST API    │  │ │
│  │  │  Manager   │ │  Hub       │ │  Endpoints   │  │ │
│  │  └─────┬──────┘ └────────────┘ └─────────────┘  │ │
│  │        │                                          │ │
│  │  ┌─────┴──────┐ ┌────────────┐ ┌─────────────┐  │ │
│  │  │  Process   │ │  Project   │ │  Event       │  │ │
│  │  │  Pool      │ │  Registry  │ │  Bus         │  │ │
│  │  │  (tmux)    │ │  (SQLite)  │ │              │  │ │
│  │  └────────────┘ └────────────┘ └─────────────┘  │ │
│  └───────────────────────────────────────────────────┘ │
│                                                       │
│  ┌───────────────────────────────────────────────────┐ │
│  │              Agent Backends (pluggable)            │ │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐          │ │
│  │  │ Claude   │ │  Codex   │ │ Future   │          │ │
│  │  │ Code CLI │ │  CLI     │ │ Agents   │          │ │
│  │  └──────────┘ └──────────┘ └──────────┘          │ │
│  └───────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Component Breakdown

**Ashlr Core** (Python, ~1500 lines)
- aiohttp web server (serves dashboard + REST API + WebSocket)
- Agent lifecycle management (spawn, monitor, send, kill, pause, resume)
- System metrics collection (CPU, RAM, disk via psutil)
- Project registry (tracks repos, working directories, agent assignments)
- Event bus (internal pub/sub for component communication)
- Configuration management (YAML-based)
- SQLite persistence (agent history, project state, event log)

**Web Dashboard** (Single HTML file, ~2000 lines)
- Agent card grid with real-time status updates
- Inline agent interaction (approve plans, answer questions, send commands)
- Full terminal view (click into any agent card)
- System metrics bar
- Voice control (Web Speech API, push-to-talk)
- Command palette (keyboard-driven quick actions)
- Dark theme, responsive, no external dependencies

**Agent Manager**
- Wraps tmux for process management
- Each agent = one tmux session running a CLI tool (claude, codex, etc.)
- Captures terminal output every ~1 second
- Parses output to detect agent status (working, waiting, error, idle, planning)
- Tracks resource usage per agent (memory, CPU)
- Supports configurable agent backends

---

## 4. TECH STACK

### Why This Stack

| Choice | Reasoning |
|--------|-----------|
| **Python 3.11+** | Best language for process management (subprocess, psutil, tmux). Claude Code iterates on Python with zero build step. Runs on Mac and Pi. |
| **aiohttp** | Async HTTP + WebSocket in one library. No framework overhead. Battle-tested. |
| **Single HTML file** | No build step, no bundler, no node_modules. Change → refresh → see result. Fastest iteration possible with Claude Code. |
| **Vanilla JS** | No React, no framework. Modern JS (ES2022+) is powerful enough. Zero dependencies = zero breakage. |
| **SQLite** | Zero-config persistence. Single file. Perfect for local-first app. |
| **tmux** | Proven terminal multiplexer. Handles process isolation, output capture, session persistence. Cross-platform. |
| **Web Speech API** | Browser-native speech recognition. Zero dependencies. Push-to-talk via keyboard shortcut. |
| **WebSocket** | Real-time bidirectional communication. Native browser support. Perfect for live agent updates. |

### Dependencies (Total: 4 Python packages)

```
aiohttp>=3.9.0       # Web server + WebSocket
aiohttp-cors>=0.7.0  # CORS support
psutil>=5.9.0        # System metrics
pyyaml>=6.0          # Configuration
```

That's it. Four dependencies. Everything else is Python stdlib or browser APIs.

### Future: Desktop App

When ready for a native desktop experience, wrap the web dashboard in **Tauri** (Rust-based, ~3MB binary). The web UI becomes the desktop app with added:
- Native window management and menubar icon
- Global keyboard shortcuts (PTT key works system-wide)
- File system access without browser restrictions
- System notifications
- Auto-start on login

This is a later phase. The web dashboard served locally is the MVP.

---

## 5. DATA MODEL

### Agent

```python
@dataclass
class Agent:
    id: str                    # Short random hex, e.g., "a7f3"
    name: str                  # User-friendly name, e.g., "auth-api"
    role: str                  # Role key, e.g., "backend"
    status: AgentStatus        # working | planning | waiting | idle | error | paused | spawning
    project_id: str | None     # Which project this agent belongs to
    working_dir: str           # Absolute path to working directory
    backend: str               # "claude-code" | "codex" | "custom"
    task: str                  # Current task description
    summary: str               # 1-2 line AI-generated summary of what agent is doing NOW
    context_used: int          # Tokens used in current context window
    context_total: int         # Total context window size
    context_pct: float         # Percentage used (0.0-1.0)
    memory_mb: float           # Current RSS memory usage
    pid: int | None            # OS process ID
    tmux_session: str          # tmux session name
    created_at: datetime
    updated_at: datetime
    output_lines: list[str]    # Last N lines of terminal output (ring buffer)
    needs_input: bool          # True if agent is waiting for user response
    input_prompt: str | None   # What the agent is asking, if needs_input
    error_message: str | None  # Error details if status is error
    tags: list[str]            # User-defined tags for organization
```

### Agent Status Model

```
SPAWNING  → Agent process is starting up
PLANNING  → Agent is analyzing/planning (yellow card)
WORKING   → Agent is actively writing code/executing (blue card, pulsing)
WAITING   → Agent needs user input (orange card, attention indicator)
IDLE      → Agent is done or has no task (gray card)
ERROR     → Agent hit an error (red card)
PAUSED    → Agent is paused by user (dim card)
```

**Status Detection** (from terminal output parsing):
- Look for patterns like "Plan:", "Thinking...", "Writing...", "> " (input prompt)
- Claude Code specific: detect plan mode, tool use, permission requests
- Configurable regex patterns per backend
- AI-powered summary extraction: periodically ask a fast model to summarize the last 50 lines of output into 1-2 sentences

### Project

```python
@dataclass
class Project:
    id: str                    # Short random hex
    name: str                  # e.g., "payment-service"
    path: str                  # Absolute path to repo root
    repo_url: str | None       # GitHub URL if applicable
    agents: list[str]          # Agent IDs assigned to this project
    created_at: datetime
    tags: list[str]
```

### Role

```python
@dataclass
class Role:
    key: str                   # e.g., "backend"
    name: str                  # e.g., "Backend Engineer"
    icon: str                  # Emoji, e.g., "⚙"
    color: str                 # Hex color for card accent, e.g., "#3B82F6"
    description: str           # Short description
    system_prompt: str         # Injected as first message when agent spawns
    max_memory_mb: int         # Resource limit
    tools: list[str]           # Allowed tools/capabilities
```

### Built-in Roles

| Key | Icon | Color | Name | Focus |
|-----|------|-------|------|-------|
| `frontend` | 🎨 | `#8B5CF6` | Frontend | React, Vue, CSS, UI/UX, accessibility |
| `backend` | ⚙ | `#3B82F6` | Backend | Python, Node, APIs, databases, auth |
| `devops` | 🚀 | `#F97316` | DevOps | Infrastructure, CI/CD, Docker, K8s |
| `tester` | 🧪 | `#22C55E` | Tester | Unit tests, integration tests, E2E, coverage |
| `reviewer` | 👁 | `#EAB308` | Reviewer | Code review, architecture review, best practices |
| `security` | 🔒 | `#EF4444` | Security | Vulnerability audit, dependency scanning |
| `architect` | 🏗 | `#06B6D4` | Architect | System design, planning, technical decisions |
| `docs` | 📝 | `#A855F7` | Docs | Documentation, READMEs, API docs, comments |
| `general` | 🤖 | `#64748B` | General | All-purpose agent, no specialization |

---

## 6. UI/UX DESIGN

### Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  ▣ ASHLR   Projects ▾   [+ Agent]         CPU 34%  RAM 62%    │  ← Top bar
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ 🎨 auth  │ │ ⚙ api    │ │ 🔒 audit │ │ 🧪 tests │           │  ← Agent cards
│  │ WORKING  │ │ WAITING  │ │ COMPLETE │ │ PLANNING │           │     (grid)
│  │ Writing  │ │ Approve? │ │ 3 issues │ │ Analyzi  │           │
│  │ ████░░░░ │ │ ██░░░░░░ │ │ ████████ │ │ █░░░░░░░ │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
│                                                                  │
│  ┌──────────┐ ┌──────────┐                                      │
│  │ 📝 docs  │ │ + New    │                                      │
│  │ WORKING  │ │  Agent   │                                      │
│  │ Writing  │ │          │                                      │
│  │ ███░░░░░ │ │          │                                      │
│  └──────────┘ └──────────┘                                      │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  🎤 PTT Ready  │  > Type a command or message...         [Send] │  ← Command bar
└─────────────────────────────────────────────────────────────────┘
```

### Agent Card Design

Each card is ~180x140px and shows:

```
┌────────────────────────┐
│ 🎨 auth-frontend    ⋮  │  ← Role icon + name + menu
│ payment-service         │  ← Project name (dimmed)
├────────────────────────┤
│                         │
│ Writing LoginForm       │  ← 1-2 line live summary
│ component with OAuth    │
│                         │
├────────────────────────┤
│ ████████████░░░░  72%   │  ← Context window usage
│ WORKING        2m ago   │  ← Status badge + time
└────────────────────────┘
```

**Card colors by status:**
- **WORKING**: Left border = role color, subtle pulse animation
- **PLANNING**: Left border = yellow/amber, thinking indicator
- **WAITING**: Left border = orange, attention pulse, shows what agent needs
- **IDLE**: Left border = gray, dimmed
- **ERROR**: Left border = red, error icon
- **PAUSED**: Entire card dimmed, pause icon overlay
- **SPAWNING**: Skeleton loading animation

### Inline Interaction (The Key Innovation)

When an agent's card shows **WAITING** status, the user can respond WITHOUT leaving the command center:

1. Card shows a truncated version of what the agent is asking
2. Click the card → it expands inline (or in a slide-out panel)
3. You see the agent's question/plan in full
4. Action buttons: **Approve**, **Reject**, **Edit**, or type a custom response
5. Your response is sent to the agent's tmux session
6. Card collapses back, agent resumes

This is critical for flow state. You should never need to "enter a terminal" for routine interactions.

### Deep View (Click Into Agent)

Clicking an agent card opens a full terminal view:

```
┌─────────────────────────────────────────────────────────────────┐
│  ← Back   🎨 auth-frontend   payment-service   WORKING         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  $ claude --dangerously-skip-permissions                         │
│                                                                  │
│  ╭──────────────────────────────────────────────────────────╮   │
│  │ I'll create the LoginForm component with OAuth support.  │   │
│  │ Let me start by examining the existing auth setup...     │   │
│  │                                                          │   │
│  │ Reading src/auth/config.ts...                            │   │
│  │ Reading src/components/AuthProvider.tsx...                │   │
│  │                                                          │   │
│  │ I can see you're using NextAuth with the Google and      │   │
│  │ GitHub providers. I'll create a LoginForm that:          │   │
│  │ 1. Shows provider buttons                                │   │
│  │ 2. Handles the OAuth redirect flow                       │   │
│  │ 3. Shows loading/error states                            │   │
│  │ 4. Matches your existing Tailwind design system          │   │
│  ╰──────────────────────────────────────────────────────────╯   │
│                                                                  │
│  Writing src/components/LoginForm.tsx...                         │
│  ████████████████████████░░░░░░░░░░                              │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  Context: ████████████░░░░  72%  │  Memory: 485 MB  │  3m 22s  │
├─────────────────────────────────────────────────────────────────┤
│  > Type a message to this agent...                       [Send] │
└─────────────────────────────────────────────────────────────────┘
```

Features of the deep view:
- Full terminal output with ANSI color rendering
- Auto-scroll (with "scroll lock" when you scroll up to read)
- Message input at the bottom (just like Claude Code in terminal)
- Context window usage, memory, and elapsed time
- Back button returns to card grid

### Command Palette

Press `Cmd+K` (or `/`) to open a command palette:

```
┌──────────────────────────────────────┐
│ > spawn backend auth-api payment-s.. │
├──────────────────────────────────────┤
│ 🚀 Spawn new agent                   │
│ 💀 Kill agent...                      │
│ ⏸  Pause all agents                  │
│ ▶  Resume all agents                 │
│ 📁 Add project...                     │
│ ⚡ Deep work session...               │
│ 🎤 Toggle voice mode                  │
│ ⚙  Settings                          │
└──────────────────────────────────────┘
```

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Cmd+K` or `/` | Command palette |
| `Cmd+N` | Spawn new agent |
| `1-9` | Focus agent by position |
| `Escape` | Back to grid / close panel |
| `Space` (hold) | Push-to-talk voice input |
| `Cmd+Enter` | Send message to focused agent |
| `Cmd+Shift+A` | Approve (when agent is waiting) |
| `Cmd+Shift+P` | Pause/Resume focused agent |
| `Cmd+Shift+K` | Kill focused agent |

---

## 7. VOICE SYSTEM

### How It Works

1. User holds **Space** (configurable PTT key)
2. Browser activates Web Speech API recognition
3. Spoken text is captured when key is released
4. Text is parsed for intent:
   - **Spawn commands**: "spin up a backend agent on payment service"
   - **Agent commands**: "approve agent 2's plan" / "kill the security agent"
   - **Status queries**: "what's the frontend agent working on?"
   - **Broadcast**: "all agents, commit your changes"
5. Parsed intent is sent to the server via WebSocket
6. Server executes the action
7. Dashboard shows visual confirmation + audio feedback (subtle tone)

### Voice Intent Parsing

The voice input is processed by a lightweight intent parser (regex + keyword matching for MVP, LLM-powered in v2):

```
"spawn a backend agent on payment service"
  → { action: "spawn", role: "backend", project: "payment-service" }

"what is agent 3 doing"
  → { action: "status", target: "agent-3" }

"approve"
  → { action: "approve", target: "focused-agent" }

"kill all idle agents"
  → { action: "kill", filter: { status: "idle" } }

"start a deep work session on the auth module"
  → { action: "workflow", template: "deep-work", target: "auth" }
```

### Audio Feedback

- **PTT activated**: Subtle low tone (like a walkie-talkie click)
- **Command recognized**: Higher confirmation tone
- **Error/unrecognized**: Different tone + visual indicator
- **Agent needs attention**: Notification sound (configurable)

Use the Web Audio API for tones — no audio files needed.

---

## 8. WEBSOCKET PROTOCOL

### Connection

Dashboard connects to `ws://localhost:5000/ws` on load. Auto-reconnects with exponential backoff.

### Server → Client Messages

```typescript
// Agent state update (sent on any agent change)
{
  type: "agent_update",
  agent: {
    id: "a7f3",
    name: "auth-api",
    role: "backend",
    status: "working",
    project_id: "p1",
    summary: "Writing JWT validation middleware for /api/auth endpoints",
    context_pct: 0.72,
    memory_mb: 485,
    needs_input: false,
    updated_at: "2026-02-24T12:42:00Z"
  }
}

// Terminal output update (sent every ~1s per active agent)
{
  type: "agent_output",
  agent_id: "a7f3",
  lines: ["Reading src/middleware/auth.ts...", "Writing src/middleware/jwt.ts..."],
  full_output: false  // true = replace all; false = append
}

// System metrics (sent every 2s)
{
  type: "metrics",
  cpu_pct: 34.2,
  memory: { total_gb: 16.0, used_gb: 9.9, available_gb: 6.1 },
  agents_active: 5,
  agents_total: 6
}

// Event notification
{
  type: "event",
  event: "agent_needs_input",  // or: agent_error, agent_complete, agent_spawned, agent_killed
  agent_id: "a7f3",
  message: "Agent is asking: Should I proceed with this migration plan?",
  timestamp: "2026-02-24T12:42:00Z"
}

// Full state sync (sent on connect and on request)
{
  type: "sync",
  agents: [...],
  projects: [...],
  config: {...}
}
```

### Client → Server Messages

```typescript
// Spawn agent
{
  type: "spawn",
  role: "backend",
  name: "auth-api",           // optional, auto-generated if omitted
  project_id: "p1",           // optional
  working_dir: "/Users/mason/Desktop/payment-service",
  task: "Implement JWT auth middleware",
  plan_mode: true              // start in plan mode
}

// Send message to agent
{
  type: "send",
  agent_id: "a7f3",
  message: "yes, proceed with the plan"
}

// Kill agent
{ type: "kill", agent_id: "a7f3" }

// Pause agent
{ type: "pause", agent_id: "a7f3" }

// Resume agent
{ type: "resume", agent_id: "a7f3" }

// Request full state sync
{ type: "sync_request" }

// Voice command (pre-parsed by dashboard)
{
  type: "voice_command",
  raw_text: "spawn a backend agent on payment service",
  parsed: { action: "spawn", role: "backend", project: "payment-service" }
}

// Register project
{
  type: "add_project",
  name: "payment-service",
  path: "/Users/mason/Desktop/payment-service"
}
```

---

## 9. AGENT OUTPUT PARSING

### Status Detection

The system periodically reads agent terminal output and parses it to determine status. This is the intelligence layer that makes the cards useful.

**Claude Code output patterns to detect:**

```python
STATUS_PATTERNS = {
    "planning": [
        r"(?i)plan:",
        r"(?i)let me (think|analyze|plan|consider)",
        r"(?i)here'?s (my|the) (plan|approach|strategy)",
        r"(?i)I'll (start by|first|begin)",
    ],
    "working": [
        r"(?i)(writing|creating|editing|reading|updating) .+\.\w+",
        r"(?i)(running|executing) .+",
        r"⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏",  # spinner characters
        r"█+░*",  # progress bar
    ],
    "waiting": [
        r"(?i)(do you want|shall I|should I|would you like)",
        r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])",
        r"(?i)proceed\?",
        r"(?i)approve",
        r"> $",  # empty prompt waiting for input
    ],
    "error": [
        r"(?i)(error|exception|traceback|failed|fatal)",
        r"(?i)command not found",
        r"(?i)permission denied",
    ],
    "complete": [
        r"(?i)(done|complete|finished|all set)",
        r"(?i)successfully",
    ],
}
```

### Summary Generation

Every 5 seconds, for each active agent, extract a 1-2 line summary:

**MVP approach** (no LLM needed): Parse the last 20 lines of output. Find the most recent "action line" (Writing X, Reading Y, Running Z). Combine with the agent's assigned task. Example: "Writing LoginForm component — OAuth integration for payment-service"

**V2 approach**: Send the last 50 lines to a fast model (Haiku) with the prompt: "In 10-15 words, what is this agent currently doing?" This gives genuinely intelligent summaries.

### Context Window Tracking

Claude Code doesn't directly expose context usage, but we can estimate:
- Count total characters of captured output
- Estimate tokens (chars / 4 rough approximation)
- Track relative to known context limits (200K for Sonnet)
- Show as a progress bar on the card

---

## 10. FILE STRUCTURE

```
ashlr/
├── ashlr_server.py          # THE server — everything in one file
├── ashlr_dashboard.html     # THE dashboard — everything in one file
├── requirements.txt          # 4 dependencies
├── ashlr.yaml              # Default configuration (auto-created)
├── start.sh                 # Launch script (install deps + start)
└── README.md                # Getting started guide
```

Yes, seriously — two files. Here's why:

1. **One Python file** means Claude Code can read the entire server in one shot, understand it fully, and make changes confidently. No import chains to trace, no file navigation overhead.

2. **One HTML file** means no build step, no bundler, no framework. The dashboard is self-contained. Claude Code edits it, you refresh the browser, done.

3. When the codebase outgrows single files (probably around v0.5), we split deliberately — but starting monolithic is the fastest path to a working system.

### The Server File (~1500-2000 lines)

```python
# ashlr_server.py — sections:

# 1. Imports and constants (~30 lines)
# 2. Configuration dataclass + loader (~80 lines)
# 3. Data models: Agent, Project, Role (~150 lines)
# 4. AgentManager: spawn, kill, pause, resume, send, capture (~400 lines)
# 5. StatusParser: detect agent status from output (~100 lines)
# 6. MetricsCollector: system metrics via psutil (~80 lines)
# 7. EventBus: internal pub/sub (~50 lines)
# 8. REST API handlers (~200 lines)
# 9. WebSocket handler (~150 lines)
# 10. Background tasks: capture, metrics, health, watchdog (~200 lines)
# 11. Application setup + main (~100 lines)
```

### The Dashboard File (~2000-2500 lines)

```html
<!-- ashlr_dashboard.html — sections: -->

<!-- 1. HTML structure (~100 lines) -->
<!-- 2. CSS: dark theme, card styles, animations (~500 lines) -->
<!-- 3. JS: WebSocket connection + reconnect (~100 lines) -->
<!-- 4. JS: State management (agents, projects, metrics) (~150 lines) -->
<!-- 5. JS: Card grid renderer (~300 lines) -->
<!-- 6. JS: Inline interaction panel (~200 lines) -->
<!-- 7. JS: Terminal deep view (~250 lines) -->
<!-- 8. JS: Command palette (~150 lines) -->
<!-- 9. JS: Voice input (Web Speech API) (~150 lines) -->
<!-- 10. JS: Keyboard shortcuts (~80 lines) -->
<!-- 11. JS: Notifications + audio feedback (~80 lines) -->
<!-- 12. JS: Project management UI (~100 lines) -->
```

---

## 11. DEVELOPMENT PHASES

### Phase 1: Foundation (Days 1-3)
**Goal**: A working server that spawns Claude Code agents and shows them in a dashboard.

Build:
- [ ] `ashlr_server.py` — aiohttp server with agent management via tmux
- [ ] `ashlr_dashboard.html` — agent card grid with WebSocket updates
- [ ] Basic REST API (spawn, kill, list agents)
- [ ] Terminal output capture and display
- [ ] System metrics bar (CPU, RAM)

Test: Can spawn 3 agents, see their cards update in real-time, click into terminal view, send a message.

### Phase 2: Interaction (Days 4-6)
**Goal**: Inline interaction so you never need to leave the dashboard.

Build:
- [ ] Status detection from terminal output (planning, working, waiting, error)
- [ ] Card color coding by status
- [ ] Inline interaction panel (approve/reject/respond when agent is waiting)
- [ ] Agent summary generation (parse output for current activity)
- [ ] Context window usage estimation + display
- [ ] Notification when agent needs attention

Test: Agent asks a question → card turns orange → click to see question → approve inline → agent resumes.

### Phase 3: Voice + Command (Days 7-9)
**Goal**: Push-to-talk voice control and command palette.

Build:
- [ ] Web Speech API integration with PTT (Space key)
- [ ] Voice intent parser (spawn, kill, approve, status queries)
- [ ] Command palette (Cmd+K)
- [ ] Keyboard shortcuts for all common actions
- [ ] Audio feedback tones
- [ ] Spawn dialog (pick role, project, task)

Test: Hold Space, say "spawn a backend agent", release. Agent spawns. Say "what's agent 1 doing" — see response.

### Phase 4: Projects + Polish (Days 10-14)
**Goal**: Project management, persistence, and production polish.

Build:
- [ ] Project registry (add/remove projects, assign agents)
- [ ] SQLite persistence (survive server restarts)
- [ ] Agent history (what did this agent do in previous sessions?)
- [ ] Workflow templates (deep-work session, audit, etc.)
- [ ] Settings panel (configure PTT key, agent limits, theme)
- [ ] Error handling + edge cases
- [ ] Performance optimization (handle 20+ agents smoothly)

Test: Full workflow — add 3 projects, spawn 8 agents across them, use voice to manage, restart server, agents resume.

### Phase 5: Intelligence (Days 15-21)
**Goal**: Ashlr becomes smart — AI-powered summaries, coordination, suggestions.

Build:
- [ ] LLM-powered agent summaries (Haiku for fast, cheap analysis)
- [ ] Smart notifications (only surface what actually needs attention)
- [ ] Agent coordination (detect when agents should share information)
- [ ] Workflow suggestions ("these 3 agents are done — want me to start testing?")
- [ ] Multi-backend support (Codex, custom agents)
- [ ] Tauri desktop wrapper (native app)

### Future: Phase 6+
- Team support (multiple users, shared dashboard)
- Cloud sync (access your command center from anywhere)
- Agent marketplace (share/download role templates)
- Hardware integration (Ashlr Compact device)
- Mobile companion app

---

## 12. CONFIGURATION

### Default ashlr.yaml

```yaml
# Ashlr Configuration
# Auto-created on first run at ~/.ashlr/ashlr.yaml

server:
  host: "127.0.0.1"
  port: 5000
  log_level: "INFO"

agents:
  max_concurrent: 16
  default_role: "general"
  default_working_dir: "~/Projects"
  output_capture_interval_sec: 1.0
  summary_update_interval_sec: 5.0
  memory_limit_mb: 2048
  memory_warn_mb: 1536

  # Agent backend configuration
  backends:
    claude-code:
      command: "claude"
      args: ["--dangerously-skip-permissions"]
      # Set to true to start agents in plan mode by default
      plan_mode: false

    codex:
      command: "codex"
      args: []
      plan_mode: false

  # Default backend for new agents
  default_backend: "claude-code"

voice:
  enabled: true
  ptt_key: "Space"       # Key to hold for push-to-talk
  feedback_sounds: true   # Play audio tones for feedback
  language: "en-US"

display:
  theme: "dark"
  cards_per_row: 4        # Auto-adjusts based on window size
  show_metrics_bar: true
  notification_sound: true

projects: []
# Projects are added via the UI or CLI:
# - name: "payment-service"
#   path: "/Users/mason/Desktop/payment-service"
```

---

## 13. GETTING STARTED (FOR CLAUDE CODE)

### To build this project:

```bash
# 1. Create the project directory
mkdir -p ~/ashlr && cd ~/ashlr

# 2. Create the two source files
# (Claude Code creates ashlr_server.py and ashlr_dashboard.html)

# 3. Install dependencies
pip install aiohttp aiohttp-cors psutil pyyaml

# 4. Ensure tmux is installed
brew install tmux  # macOS
# sudo apt install tmux  # Linux

# 5. Run the server
python ashlr_server.py

# 6. Open browser
open http://localhost:5000
```

### Development workflow with Claude Code:

1. Open the ashlr directory in Claude Code
2. Start with Phase 1 — get the basic server + dashboard working
3. Test manually (spawn agents, watch cards update)
4. Iterate on each phase
5. The architecture doc (this file) is the source of truth

### Key implementation notes for Claude Code:

- **Start with the server**. Get `ashlr_server.py` working first — spawn a tmux session, capture output, serve it via WebSocket. The dashboard is useless without data.
- **Test with fake agents first**. Before requiring Claude Code CLI to be installed, support a "demo mode" that spawns bash sessions with simulated agent behavior (just echo commands with delays).
- **The dashboard must be a SINGLE HTML file** served by the Python server at `/`. No separate static file serving. The HTML is either embedded in the Python file as a string, or loaded from disk at startup.
- **WebSocket is the primary communication channel**. REST is for one-off requests. All real-time updates go through WebSocket.
- **tmux is non-negotiable for MVP**. It handles process isolation, output capture, and session persistence. Don't try to reinvent this with raw subprocess.

---

## 14. WHAT SUCCESS LOOKS LIKE

When Ashlr is done right, this is the experience:

> I open Ashlr. My three active projects load with their agent configurations from yesterday. I hold Space: "Start a deep work session on payment-service — audit, plan, and propose improvements." Three agents spin up. Their cards appear, turn yellow as they start planning.
>
> I switch to my email for 10 minutes.
>
> I come back. Two agents have plans ready — their cards are orange, pulsing gently. The third found a critical bug and its card is red. I click the red card first, read the bug report, say "fix it." Click the first orange card, review the plan, hit Approve. Click the second, it's a refactor plan — I say "looks good but skip the database migration for now" and hit Send.
>
> All three agents are now working. Blue cards, pulsing. I can see their summaries updating: "Writing JWT middleware...", "Fixing SQL injection in user query...", "Refactoring payment processor class..."
>
> I hold Space: "How's memory looking?" The metrics bar highlights — 62% RAM used across 3 agents. Comfortable.
>
> Agent 2 finishes the bug fix. Card turns green. I click in, see the diff, say "commit and push." Done. I hold Space: "Spin up a tester agent to verify the fix." A new card appears.
>
> I never opened a terminal. I never lost context. I managed 4 agents across a complex codebase in 15 minutes while also checking email. This is the flow state.

---

*This document is the single source of truth for the Ashlr project. When in doubt, refer here. When building in Claude Code, reference this for architecture decisions, data models, and UI patterns.*
