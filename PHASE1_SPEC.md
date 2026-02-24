# Phase 1: Foundation — Implementation Spec

**Goal**: A working server that spawns Claude Code agents via tmux and shows them as live-updating cards in a web dashboard.

**Done when**: You can open http://localhost:5000, click "New Agent", pick a role and working directory, see the agent card appear, watch its status update in real-time, click into it for full terminal output, and send it a message.

---

## File 1: ashlar_server.py

### Section 1: Imports & Setup (~50 lines)

```python
import asyncio, json, logging, os, re, shutil, signal, subprocess, time, uuid, sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any
import aiohttp
from aiohttp import web
import psutil
import yaml
```

- Colored console logging + file logging to ~/.ashlar/ashlar.log
- ASCII banner on startup
- Check for tmux (required), claude CLI (optional, warn if missing)

### Section 2: Configuration (~80 lines)

```python
@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 5000
    max_agents: int = 16
    default_role: str = "general"
    default_working_dir: str = "~/Projects"
    output_capture_interval: float = 1.0
    memory_limit_mb: int = 2048
    claude_command: str = "claude"
    claude_args: list = field(default_factory=lambda: ["--dangerously-skip-permissions"])
    demo_mode: bool = False  # True if claude CLI not found
```

- Load from ~/.ashlar/ashlar.yaml (create with defaults if missing)
- Expand ~ in paths
- Set demo_mode=True if claude CLI not found

### Section 3: Data Models (~120 lines)

**Agent** dataclass (see CLAUDE.md for full fields)

**Role** dataclass:
```python
@dataclass
class Role:
    key: str
    name: str
    icon: str
    color: str
    description: str
    system_prompt: str
    max_memory_mb: int = 2048
```

**BUILTIN_ROLES** dict with 9 roles. Each role has a SHORT but effective system prompt (3-5 sentences, not the massive ones from the old code). The prompt is injected as the first message to the claude session.

**SystemMetrics** dataclass for CPU, memory, disk.

### Section 4: AgentManager (~400 lines) — THE CORE

This is the most important class. It must work perfectly.

```python
class AgentManager:
    def __init__(self, config: Config):
        self.config = config
        self.agents: dict[str, Agent] = {}
        self.tmux_prefix = "ashlar"

    async def spawn(self, role: str, name: str | None, working_dir: str,
                    task: str, plan_mode: bool = False) -> Agent:
        """Spawn a new agent. Returns the Agent object."""
        # 1. Generate ID (4 hex chars)
        # 2. Generate name if not provided (role + short descriptor)
        # 3. Create tmux session:
        #    tmux new-session -d -s ashlar-{id} -x 200 -y 50 -c {working_dir}
        # 4. If demo_mode: send 'bash' to tmux
        #    Else: send 'claude {args}' to tmux
        #    If plan_mode, add '--plan' flag or send /plan after startup
        # 5. If role has system_prompt, wait 2s then send it as first message
        # 6. If task provided, wait 1s then send task
        # 7. Find PID of the claude/bash process inside tmux
        # 8. Create Agent object, add to self.agents
        # 9. Return agent

    async def kill(self, agent_id: str) -> bool:
        """Kill an agent gracefully."""
        # 1. Send '/exit' to tmux session
        # 2. Wait 3 seconds
        # 3. tmux kill-session -t ashlar-{id}
        # 4. Remove from self.agents

    async def pause(self, agent_id: str) -> bool:
        """Pause agent by sending Ctrl+C."""
        # tmux send-keys -t ashlar-{id} C-c
        # Set agent.status = "paused"

    async def resume(self, agent_id: str, message: str | None = None) -> bool:
        """Resume a paused agent."""
        # If message: send message
        # Else: send the agent's original task again
        # Set agent.status = "working"

    async def send_message(self, agent_id: str, message: str) -> bool:
        """Send a message/command to an agent's tmux session."""
        # tmux send-keys -t ashlar-{id} '{message}' Enter
        # Handle multi-line by splitting on \n and sending each line

    async def capture_output(self, agent_id: str) -> list[str]:
        """Capture recent terminal output from agent's tmux pane."""
        # tmux capture-pane -t ashlar-{id} -p -S -200
        # Parse output, strip empty trailing lines
        # Update agent.output_lines (keep last 500 lines as ring buffer)
        # Return new lines since last capture

    async def detect_status(self, agent_id: str) -> str:
        """Analyze output to detect agent's current status."""
        # Look at last 20 lines of output
        # Match against STATUS_PATTERNS
        # Detect needs_input (prompt waiting for yes/no/approval)
        # Return new status string

    async def get_agent_memory(self, agent_id: str) -> float:
        """Get RSS memory of agent's process tree in MB."""
        # Use psutil to find process by PID
        # Sum memory of process + all children

    def cleanup_all(self):
        """Kill all tmux sessions on shutdown. Called synchronously."""
        # List all tmux sessions starting with 'ashlar-'
        # Kill each one
```

**Critical implementation details:**

1. `tmux send-keys` needs proper escaping. Use `subprocess.run(["tmux", "send-keys", "-t", session, message, "Enter"])` — do NOT use shell=True.

2. For output capture, `tmux capture-pane -p` outputs to stdout. Capture with `subprocess.run(..., capture_output=True, text=True)`.

3. PID detection: After spawning, run `tmux list-panes -t ashlar-{id} -F '#{pane_pid}'` to get the shell PID, then use psutil to find the claude child process.

4. Demo mode: Instead of `claude`, spawn `bash` and echo simulated output periodically using a small shell script that mimics agent behavior.

### Section 5: Status Parser (~80 lines)

```python
STATUS_PATTERNS = {
    "planning": [
        r"(?i)\bplan\b",
        r"(?i)let me (think|analyze|plan|consider)",
        r"(?i)here'?s (my|the) (plan|approach)",
    ],
    "working": [
        r"(?i)(writing|creating|reading|editing|updating) \S+\.\w+",
        r"(?i)(running|executing)",
        r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]",  # spinner chars
    ],
    "waiting": [
        r"(?i)(do you want|shall I|should I|would you like)",
        r"(?i)\?([\s]*$)",
        r"(?i)(yes/no|y/n|\[Y/n\])",
        r"(?i)proceed\?",
    ],
    "error": [
        r"(?i)\b(error|exception|traceback|failed|fatal)\b",
        r"(?i)command not found",
    ],
}
```

Also detect `needs_input` separately — if the last non-empty line ends with `?` or matches waiting patterns, set `needs_input = True` and extract the question as `input_prompt`.

### Section 6: Metrics Collector (~60 lines)

```python
async def collect_metrics() -> dict:
    cpu_pct = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return {
        "cpu_pct": cpu_pct,
        "memory": {"total_gb": round(mem.total/1e9,1), "used_gb": round(mem.used/1e9,1), "available_gb": round(mem.available/1e9,1), "pct": mem.percent},
        "disk": {"total_gb": round(disk.total/1e9,1), "used_gb": round(disk.used/1e9,1), "pct": disk.percent},
        "load_avg": os.getloadavg(),
    }
```

### Section 7: WebSocket Handler (~150 lines)

```python
class WebSocketHub:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)

        # Send full state sync on connect
        await ws.send_json({"type": "sync", "agents": [...], "projects": [...], "config": {...}})

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    await self.handle_message(data, ws)
        finally:
            self.clients.discard(ws)
        return ws

    async def handle_message(self, data: dict, ws):
        match data.get("type"):
            case "spawn": ...
            case "send": ...
            case "kill": ...
            case "pause": ...
            case "resume": ...
            case "sync_request": ...

    async def broadcast(self, message: dict):
        """Send to all connected clients."""
        dead = set()
        for client in self.clients:
            try:
                await client.send_json(message)
            except (ConnectionError, RuntimeError):
                dead.add(client)
        self.clients -= dead
```

### Section 8: REST API (~150 lines)

Standard aiohttp route handlers. Each one delegates to AgentManager and returns JSON. Include proper error handling (404 for unknown agent_id, 400 for bad input, 503 if max agents reached).

### Section 9: Background Tasks (~150 lines)

```python
async def output_capture_loop(app):
    """Capture output from all agents every ~1 second."""
    manager = app["agent_manager"]
    hub = app["ws_hub"]
    while True:
        for agent_id, agent in list(manager.agents.items()):
            if agent.status in ("paused", "idle"):
                continue
            new_lines = await manager.capture_output(agent_id)
            if new_lines:
                await hub.broadcast({"type": "agent_output", "agent_id": agent_id, "lines": new_lines})

            new_status = await manager.detect_status(agent_id)
            if new_status != agent.status:
                agent.status = new_status
                agent.updated_at = datetime.now()
                await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

                if agent.needs_input:
                    await hub.broadcast({"type": "event", "event": "agent_needs_input", "agent_id": agent_id, "message": agent.input_prompt})

        await asyncio.sleep(1.0)

async def metrics_loop(app):
    """Collect and broadcast system metrics every 2 seconds."""
    hub = app["ws_hub"]
    while True:
        metrics = await collect_metrics()
        metrics["agents_active"] = sum(1 for a in app["agent_manager"].agents.values() if a.status in ("working", "planning"))
        metrics["agents_total"] = len(app["agent_manager"].agents)
        await hub.broadcast({"type": "metrics", **metrics})
        await asyncio.sleep(2.0)

async def health_check_loop(app):
    """Verify tmux sessions are alive, clean up dead agents."""
    # Every 5 seconds, check each agent's tmux session exists
    # If tmux session is gone, set status to "error" and notify

async def memory_watchdog_loop(app):
    """Check per-agent memory usage, warn/kill if over limit."""
    # Every 10 seconds, check memory
    # If agent > config.memory_limit_mb, send warning then kill
```

### Section 10: Application Setup & Main (~80 lines)

```python
async def create_app() -> web.Application:
    app = web.Application()

    config = load_config()
    app["config"] = config
    app["agent_manager"] = AgentManager(config)
    app["ws_hub"] = WebSocketHub()

    # Routes
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/ws", app["ws_hub"].handle)
    app.router.add_get("/api/agents", list_agents)
    app.router.add_post("/api/agents", spawn_agent)
    # ... etc

    # Background tasks
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup)

    return app

def main():
    print_banner()
    check_dependencies()
    app = asyncio.run(create_app())
    web.run_app(app, host=config.host, port=config.port)
```

---

## File 2: ashlar_dashboard.html

### Structure

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Ashlar AO</title>
    <style>/* ALL CSS HERE */</style>
</head>
<body>
    <!-- Top Bar -->
    <!-- Agent Grid -->
    <!-- Inline Interaction Panel (hidden by default) -->
    <!-- Deep View (hidden by default) -->
    <!-- Command Palette (hidden by default) -->
    <!-- Spawn Dialog (hidden by default) -->
    <!-- Command Bar (bottom) -->

    <script>/* ALL JS HERE */</script>
</body>
</html>
```

### CSS (~500 lines)

**Theme**: Dark background (#0f1117), card backgrounds (#1a1d27), borders (#2a2d3a), text (#e2e8f0), dimmed text (#94a3b8). Accent colors per role.

**Key styles:**
- `.agent-grid` — CSS Grid, auto-fill, minmax(200px, 1fr)
- `.agent-card` — Rounded corners, left border (4px, role color), subtle shadow, hover effect
- `.agent-card[data-status="waiting"]` — Orange border + pulse animation
- `.agent-card[data-status="working"]` — Subtle glow animation
- `.agent-card[data-status="error"]` — Red border
- `.top-bar` — Fixed top, dark bg, flex with logo + metrics
- `.command-bar` — Fixed bottom, dark bg, input + voice button
- `.context-bar` — Small progress bar inside card (green → yellow → red)
- `.deep-view` — Full screen overlay with terminal-style monospace output
- `.command-palette` — Centered modal with search input + action list
- `.spawn-dialog` — Modal with role picker, directory input, task input
- Smooth transitions on all state changes (0.2s ease)
- `@keyframes pulse` for attention states

### JavaScript (~1500 lines)

#### State Management (~100 lines)
```javascript
const state = {
    agents: new Map(),      // id → agent object
    projects: new Map(),    // id → project object
    metrics: {},            // latest system metrics
    config: {},             // server config
    focusedAgentId: null,   // currently focused agent (for deep view)
    view: 'grid',           // 'grid' | 'deep' | 'spawn' | 'palette'
};
```

#### WebSocket Connection (~100 lines)
```javascript
class AshlarSocket {
    constructor() {
        this.ws = null;
        this.reconnectDelay = 1000;
    }
    connect() {
        this.ws = new WebSocket(`ws://${location.host}/ws`);
        this.ws.onmessage = (e) => this.handleMessage(JSON.parse(e.data));
        this.ws.onclose = () => setTimeout(() => this.connect(), this.reconnectDelay);
        // Exponential backoff on reconnect, reset on successful connect
    }
    send(data) { this.ws?.send(JSON.stringify(data)); }
    handleMessage(data) {
        switch(data.type) {
            case 'sync': handleSync(data); break;
            case 'agent_update': handleAgentUpdate(data.agent); break;
            case 'agent_output': handleAgentOutput(data); break;
            case 'metrics': handleMetrics(data); break;
            case 'event': handleEvent(data); break;
        }
    }
}
```

#### Card Grid Renderer (~300 lines)
```javascript
function renderAgentGrid() {
    // For each agent in state.agents:
    //   - Create or update card element
    //   - Set data-status attribute (drives CSS)
    //   - Update role icon, name, project, summary
    //   - Update context bar width + color
    //   - Update status badge text + time
    //   - If needs_input: show truncated question + "Respond" button
    //   - Bind click handler to open deep view
    // Add "+ New Agent" card at the end
    // Remove cards for agents that no longer exist
}
```

Use DOM diffing — don't recreate cards on every update. Update text content and attributes of existing elements. This keeps animations smooth and avoids flicker.

#### Inline Interaction (~150 lines)
When agent.needs_input is true:
- Card shows the question truncated to 2 lines
- Click "Respond" → slide-out panel appears with:
  - Full question text
  - Approve / Reject buttons (if it looks like a yes/no question)
  - Text input for custom response
  - Send button
- Sending a response: `socket.send({type: "send", agent_id, message})`

#### Deep View (~200 lines)
Full-screen terminal output viewer:
- Monospace font, dark background
- ANSI color code parsing (basic: bold, colors 0-7, bright colors)
- Auto-scroll to bottom (disabled when user scrolls up, re-enabled on "Jump to bottom" button)
- Message input at bottom
- Back button (or Escape) returns to grid
- Shows: agent name, role, status, context %, memory, elapsed time

#### Command Palette (~150 lines)
- Fuzzy search over available actions
- Actions: Spawn (by role), Kill, Pause, Resume, Focus (by agent name), Settings
- Arrow keys to navigate, Enter to select, Escape to close

#### Voice Input (~120 lines)
```javascript
class VoiceInput {
    constructor() {
        this.recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
        this.recognition.lang = 'en-US';
        this.recognition.interimResults = false;
        this.isListening = false;
    }
    startListening() {
        this.recognition.start();
        this.isListening = true;
        // Show visual indicator (mic icon turns red, "Listening..." text)
    }
    stopListening() {
        this.recognition.stop();
        this.isListening = false;
    }
    // Bind to Space key: keydown → start, keyup → stop
    // On result: parse intent, send to server, show confirmation
}
```

PTT: Hold Space to start, release to stop. Only activate when no text input is focused.

#### Audio Feedback (~50 lines)
```javascript
function playTone(freq, duration, type = 'sine') {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.value = freq;
    gain.gain.value = 0.1;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.stop(ctx.currentTime + duration);
}
// PTT start: playTone(440, 0.1)
// Command recognized: playTone(660, 0.15)
// Error: playTone(220, 0.3)
```

#### Keyboard Shortcuts (~60 lines)
Bind at document level. Check that no input is focused before handling.

#### Spawn Dialog (~100 lines)
Modal with:
- Role selector (grid of role icons + names, click to select)
- Agent name input (optional, auto-generated)
- Working directory input (with browse button that could use a native file picker via API)
- Task textarea
- "Plan mode" toggle
- "Spawn" button

---

## Testing Phase 1

### Manual Test Checklist
1. [ ] `./start.sh` installs deps and starts server
2. [ ] Browser opens to http://localhost:5000, sees empty grid with "+ New Agent"
3. [ ] Click "+ New Agent" → spawn dialog opens
4. [ ] Select "general" role, enter task "list the files in this directory", click Spawn
5. [ ] Agent card appears with SPAWNING status → transitions to WORKING
6. [ ] Card shows 1-2 line summary of what agent is doing
7. [ ] Click card → deep view shows full terminal output
8. [ ] Type a message in deep view → message appears in agent's terminal
9. [ ] Press Escape → returns to grid
10. [ ] System metrics (CPU, RAM) update in top bar
11. [ ] Kill agent from card menu → card disappears
12. [ ] Spawn 3 agents simultaneously → all cards appear and update independently
13. [ ] Ctrl+C the server → all tmux sessions are cleaned up

### Automated Verification
After building, run these checks:
```bash
# Server starts without errors
python ashlar_server.py &
sleep 2

# Health check
curl -s http://localhost:5000/api/system | python -m json.tool

# Spawn an agent
curl -s -X POST http://localhost:5000/api/agents \
  -H "Content-Type: application/json" \
  -d '{"role":"general","task":"echo hello","working_dir":"/tmp"}'

# List agents
curl -s http://localhost:5000/api/agents | python -m json.tool

# Dashboard loads
curl -s http://localhost:5000 | head -5  # Should show <!DOCTYPE html>

# Cleanup
kill %1
```

---

## What NOT to Build in Phase 1

- No SQLite persistence (agents are in-memory only)
- No project registry (just working_dir per agent)
- No voice commands (just the UI placeholder)
- No AI-powered summaries (just regex-based status detection)
- No settings panel
- No workflow templates
- No multi-backend support (just claude-code + demo mode)
