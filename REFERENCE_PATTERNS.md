# Reference Patterns — Proven Code From Prior Work

These are battle-tested patterns extracted from the existing Ashlr codebase. Use these as a starting point — don't copy verbatim, but use the patterns and approaches.

---

## 1. tmux Session Management (from agent_manager.py)

This pattern works reliably on both macOS and Linux:

```python
import subprocess
import shutil

class TmuxManager:
    """Manages tmux sessions for agent processes."""

    def __init__(self, prefix: str = "ashlr"):
        self.prefix = prefix
        self.available = shutil.which("tmux") is not None

    def create_session(self, session_id: str, working_dir: str, width: int = 200, height: int = 50) -> bool:
        """Create a detached tmux session."""
        name = f"{self.prefix}-{session_id}"
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-x", str(width), "-y", str(height), "-c", working_dir],
                check=True, timeout=5, capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            return False

    def send_keys(self, session_id: str, text: str) -> bool:
        """Send keystrokes to a tmux session. Handles Enter automatically."""
        name = f"{self.prefix}-{session_id}"
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", name, text, "Enter"],
                check=True, timeout=5, capture_output=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def send_ctrl_c(self, session_id: str) -> bool:
        """Send Ctrl+C to interrupt."""
        name = f"{self.prefix}-{session_id}"
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", name, "C-c"],
                check=True, timeout=5, capture_output=True
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def capture_pane(self, session_id: str, lines: int = 200) -> list[str]:
        """Capture visible terminal output."""
        name = f"{self.prefix}-{session_id}"
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.splitlines()
            return []
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return []

    def kill_session(self, session_id: str) -> bool:
        """Kill a tmux session."""
        name = f"{self.prefix}-{session_id}"
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                timeout=5, capture_output=True
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return True  # Already dead is fine

    def session_exists(self, session_id: str) -> bool:
        """Check if a tmux session is still alive."""
        name = f"{self.prefix}-{session_id}"
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", name],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    def get_pane_pid(self, session_id: str) -> int | None:
        """Get the PID of the process running in the tmux pane."""
        name = f"{self.prefix}-{session_id}"
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split('\n')[0])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
        return None

    def list_sessions(self) -> list[str]:
        """List all ashlr tmux sessions."""
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return [s for s in result.stdout.strip().split('\n') if s.startswith(self.prefix)]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        return []

    def cleanup_all(self):
        """Kill all ashlr sessions. Called on shutdown."""
        for session_name in self.list_sessions():
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", session_name],
                    timeout=5, capture_output=True
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
```

**Key lessons:**
- Always use `capture_output=True` to prevent tmux from polluting stdout
- Always set `timeout=5` — tmux can hang on broken sessions
- `kill_session` returning True on CalledProcessError is intentional (already dead = success)
- `send-keys` with separate `text` and `"Enter"` args is more reliable than `text + "\n"`

---

## 2. System Metrics Collection (from battery_monitor.py + ashlr_server.py)

```python
import psutil
import os

async def collect_system_metrics() -> dict:
    """Collect system-wide metrics. Non-blocking."""
    cpu = psutil.cpu_percent(interval=None)  # Non-blocking if called periodically
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')

    return {
        "cpu_pct": round(cpu, 1),
        "cpu_count": psutil.cpu_count(),
        "memory": {
            "total_gb": round(mem.total / 1e9, 1),
            "used_gb": round(mem.used / 1e9, 1),
            "available_gb": round(mem.available / 1e9, 1),
            "pct": round(mem.percent, 1),
        },
        "disk": {
            "total_gb": round(disk.total / 1e9, 1),
            "used_gb": round(disk.used / 1e9, 1),
            "pct": round(disk.percent, 1),
        },
        "load_avg": [round(x, 2) for x in os.getloadavg()],
    }

def get_process_memory_mb(pid: int) -> float:
    """Get total RSS memory of a process and all its children in MB."""
    try:
        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return round(total / 1e6, 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0
```

**Key lesson:** Call `psutil.cpu_percent(interval=None)` — the `interval=None` makes it non-blocking. It returns the CPU usage since the last call. Initialize it once at startup with `psutil.cpu_percent()` so subsequent calls have a baseline.

---

## 3. aiohttp WebSocket + HTTP Server Pattern

```python
from aiohttp import web
import aiohttp
import json

class WebSocketHub:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self.clients.add(ws)

        try:
            # Send initial state
            await ws.send_json({"type": "sync", "data": get_full_state()})

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self.route_message(data, ws)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except Exception as e:
            logging.error(f"WebSocket error: {e}")
        finally:
            self.clients.discard(ws)

        return ws

    async def broadcast(self, message: dict):
        """Send message to all connected clients. Remove dead connections."""
        if not self.clients:
            return
        dead = set()
        for ws in self.clients:
            try:
                await ws.send_json(message)
            except (ConnectionError, RuntimeError, ConnectionResetError):
                dead.add(ws)
        self.clients -= dead

# Application setup
async def create_app():
    app = web.Application()
    hub = WebSocketHub()
    app["hub"] = hub

    # Serve dashboard
    async def serve_dashboard(request):
        dashboard_path = Path(__file__).parent / "ashlr_dashboard.html"
        return web.FileResponse(dashboard_path)

    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/ws", hub.handle_ws)

    # Background tasks
    async def start_tasks(app):
        app["bg_tasks"] = [
            asyncio.create_task(output_loop(app)),
            asyncio.create_task(metrics_loop(app)),
        ]

    async def cleanup(app):
        for task in app.get("bg_tasks", []):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(start_tasks)
    app.on_cleanup.append(cleanup)

    return app
```

**Key lessons:**
- `heartbeat=30.0` on WebSocketResponse keeps connections alive
- Always `discard` dead clients in broadcast (don't raise)
- Background tasks should be created in `on_startup`, cancelled in `on_cleanup`
- Serve dashboard as `web.FileResponse` from the same directory as the server

---

## 4. Graceful Shutdown Pattern

```python
import signal
import asyncio

def setup_signal_handlers(agent_manager):
    """Ensure all tmux sessions are cleaned up on exit."""
    def handle_shutdown(signum, frame):
        print("\n→ Shutting down Ashlr...")
        agent_manager.cleanup_all()
        print("✓ All agent sessions cleaned up")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
```

---

## 5. Config Management Pattern

```python
from pathlib import Path
import yaml

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 5000, "log_level": "INFO"},
    "agents": {
        "max_concurrent": 16,
        "default_role": "general",
        "default_working_dir": "~/Projects",
        "output_capture_interval_sec": 1.0,
        "memory_limit_mb": 2048,
        "default_backend": "claude-code",
        "backends": {
            "claude-code": {"command": "claude", "args": ["--dangerously-skip-permissions"]},
            "codex": {"command": "codex", "args": []},
        },
    },
    "voice": {"enabled": True, "ptt_key": "Space", "feedback_sounds": True},
    "display": {"theme": "dark", "cards_per_row": 4},
}

def load_config() -> dict:
    config_dir = Path.home() / ".ashlr"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "ashlr.yaml"

    if config_path.exists():
        with open(config_path) as f:
            user_config = yaml.safe_load(f) or {}
        # Deep merge user config over defaults
        return deep_merge(DEFAULT_CONFIG, user_config)
    else:
        # Write defaults
        with open(config_path, 'w') as f:
            yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
        return DEFAULT_CONFIG.copy()

def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
```

---

## 6. ANSI Color Parsing for Terminal Output (for dashboard)

```javascript
// Minimal ANSI parser for terminal output display
function ansiToHtml(text) {
    const COLORS = {
        '30': '#4a4a4a', '31': '#ef4444', '32': '#22c55e', '33': '#eab308',
        '34': '#3b82f6', '35': '#a855f7', '36': '#06b6d4', '37': '#e2e8f0',
        '90': '#6b7280', '91': '#f87171', '92': '#4ade80', '93': '#facc15',
        '94': '#60a5fa', '95': '#c084fc', '96': '#22d3ee', '97': '#ffffff',
    };

    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\x1b\[(\d+(?:;\d+)*)m/g, (match, codes) => {
            const parts = codes.split(';');
            let style = '';
            for (const code of parts) {
                if (code === '0') return '</span>';
                if (code === '1') style += 'font-weight:bold;';
                if (code === '3') style += 'font-style:italic;';
                if (code === '4') style += 'text-decoration:underline;';
                if (COLORS[code]) style += `color:${COLORS[code]};`;
                // Background colors (40-47, 100-107)
                const bgCode = String(parseInt(code) - 10);
                if (parseInt(code) >= 40 && parseInt(code) <= 47 && COLORS[bgCode])
                    style += `background:${COLORS[bgCode]};`;
            }
            return style ? `<span style="${style}">` : '';
        })
        // Strip remaining escape sequences
        .replace(/\x1b\[[^m]*m/g, '')
        .replace(/\x1b\[\?[0-9;]*[a-zA-Z]/g, '')
        .replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
}
```

---

## 7. Role Definitions (Condensed)

The prior code had massive system prompts (150+ lines each). For Ashlr AO, keep them short and effective — the agent is already Claude Code, it doesn't need extensive instructions. Just give it a persona and focus area:

```python
BUILTIN_ROLES = {
    "frontend": Role(
        key="frontend", name="Frontend Engineer", icon="🎨", color="#8B5CF6",
        description="React, Vue, CSS, UI/UX, accessibility",
        system_prompt="You are a frontend specialist. Focus on component architecture, responsive design, accessibility (WCAG), and performance. Prefer TypeScript, functional components, and Tailwind. Write tests for all components.",
    ),
    "backend": Role(
        key="backend", name="Backend Engineer", icon="⚙", color="#3B82F6",
        description="APIs, databases, Python, Node.js, auth",
        system_prompt="You are a backend specialist. Focus on API design, database schemas, auth, and error handling. Write clean, well-tested code with proper validation and logging. Prefer async patterns.",
    ),
    "devops": Role(
        key="devops", name="DevOps Engineer", icon="🚀", color="#F97316",
        description="Infrastructure, CI/CD, Docker, deployment",
        system_prompt="You are a DevOps specialist. Focus on infrastructure as code, CI/CD pipelines, containerization, monitoring, and deployment automation. Prioritize reliability and observability.",
    ),
    "tester": Role(
        key="tester", name="QA Engineer", icon="🧪", color="#22C55E",
        description="Unit tests, integration tests, E2E, coverage",
        system_prompt="You are a QA specialist. Write comprehensive tests: unit, integration, and E2E. Aim for high coverage on critical paths. Test edge cases, error conditions, and race conditions.",
    ),
    "reviewer": Role(
        key="reviewer", name="Code Reviewer", icon="👁", color="#EAB308",
        description="Code review, best practices, architecture",
        system_prompt="You are a code reviewer. Audit code for bugs, security issues, performance problems, and maintainability. Be thorough but constructive. Suggest specific improvements with code examples.",
    ),
    "security": Role(
        key="security", name="Security Auditor", icon="🔒", color="#EF4444",
        description="Vulnerability audit, dependency scanning, hardening",
        system_prompt="You are a security specialist. Audit for vulnerabilities: injection, XSS, CSRF, auth bypass, secrets exposure, dependency CVEs. Provide severity ratings and specific fix recommendations.",
    ),
    "architect": Role(
        key="architect", name="Architect", icon="🏗", color="#06B6D4",
        description="System design, planning, technical decisions",
        system_prompt="You are a systems architect. Focus on high-level design, component boundaries, data flow, scalability, and technical tradeoffs. Create clear plans before implementation. Document decisions.",
    ),
    "docs": Role(
        key="docs", name="Documentation", icon="📝", color="#A855F7",
        description="READMEs, API docs, inline comments, guides",
        system_prompt="You are a documentation specialist. Write clear, concise docs: READMEs, API references, inline comments, architecture guides. Focus on the 'why' not just the 'what'. Include examples.",
    ),
    "general": Role(
        key="general", name="General", icon="🤖", color="#64748B",
        description="All-purpose agent, no specialization",
        system_prompt="You are a skilled software engineer. Approach tasks methodically: understand the requirement, explore the codebase, plan your approach, implement, and verify. Ask clarifying questions when needed.",
    ),
}
```
