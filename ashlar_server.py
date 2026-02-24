#!/usr/bin/env python3
"""
Ashlar AO — Agent Orchestration Server

Single-file aiohttp server that manages AI coding agents via tmux,
serves the web dashboard, and provides REST + WebSocket APIs.
"""

# ─────────────────────────────────────────────
# Section 1: Imports, Logging, Banner
# ─────────────────────────────────────────────

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
from aiohttp import web
import aiohttp_cors
import psutil
import yaml

# ── Logging ──

ASHLAR_DIR = Path.home() / ".ashlar"
ASHLAR_DIR.mkdir(exist_ok=True)

LOG_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[35m",  # magenta
}
RESET = "\033[0m"


class ColoredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = LOG_COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname:<8}{RESET}"
        return super().format(record)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler with colors
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(ColoredFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(ch)

    # File handler
    fh = logging.FileHandler(ASHLAR_DIR / "ashlar.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(fh)


log = logging.getLogger("ashlar")


def print_banner() -> None:
    print("\n\033[36m", end="")
    print("  ╔═══════════════════════════════════╗")
    print("  ║         A S H L A R   A O        ║")
    print("  ║     Agent Orchestration Platform   ║")
    print("  ╚═══════════════════════════════════╝")
    print(f"\033[0m")


def check_dependencies() -> bool:
    """Check for required (tmux) and optional (claude) dependencies.
    Returns True if claude CLI is found, False for demo mode."""
    if not shutil.which("tmux"):
        log.critical("tmux is required but not found. Install: brew install tmux")
        sys.exit(1)
    log.info("tmux found")

    if shutil.which("claude") and not os.environ.get("CLAUDECODE"):
        log.info("claude CLI found")
        return True
    elif os.environ.get("CLAUDECODE"):
        log.warning("Running inside Claude Code session — using demo mode to avoid nested sessions")
        return False
    else:
        log.warning("claude CLI not found — agents will run in demo mode (bash)")
        return False


# ─────────────────────────────────────────────
# Section 2: Configuration
# ─────────────────────────────────────────────

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


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 5000
    log_level: str = "INFO"
    max_agents: int = 16
    default_role: str = "general"
    default_working_dir: str = "~/Projects"
    output_capture_interval: float = 1.0
    memory_limit_mb: int = 2048
    claude_command: str = "claude"
    claude_args: list = field(default_factory=lambda: ["--dangerously-skip-permissions"])
    demo_mode: bool = False

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "max_agents": self.max_agents,
            "default_role": self.default_role,
            "default_working_dir": self.default_working_dir,
            "output_capture_interval": self.output_capture_interval,
            "memory_limit_mb": self.memory_limit_mb,
            "demo_mode": self.demo_mode,
        }


def load_config(has_claude: bool = True) -> Config:
    config_dir = ASHLAR_DIR
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "ashlar.yaml"

    # Also check for config in the project directory
    local_config = Path(__file__).parent / "ashlar.yaml"

    raw = DEFAULT_CONFIG.copy()

    if config_path.exists():
        try:
            with open(config_path) as f:
                user_config = yaml.safe_load(f) or {}
            raw = deep_merge(raw, user_config)
        except Exception as e:
            log.warning(f"Failed to load config from {config_path}: {e}")
    elif local_config.exists():
        # Copy local config to ~/.ashlar/ on first run
        try:
            shutil.copy2(local_config, config_path)
            with open(config_path) as f:
                user_config = yaml.safe_load(f) or {}
            raw = deep_merge(raw, user_config)
            log.info(f"Copied config to {config_path}")
        except Exception as e:
            log.warning(f"Failed to copy config: {e}")
    else:
        # Write defaults
        try:
            with open(config_path, "w") as f:
                yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
            log.info(f"Created default config at {config_path}")
        except Exception as e:
            log.warning(f"Failed to write default config: {e}")

    server = raw.get("server", {})
    agents = raw.get("agents", {})
    backends = agents.get("backends", {})
    claude_backend = backends.get("claude-code", {})

    default_wd = agents.get("default_working_dir", "~/Projects")
    default_wd = os.path.expanduser(default_wd)

    return Config(
        host=server.get("host", "127.0.0.1"),
        port=server.get("port", 5000),
        log_level=server.get("log_level", "INFO"),
        max_agents=agents.get("max_concurrent", 16),
        default_role=agents.get("default_role", "general"),
        default_working_dir=default_wd,
        output_capture_interval=agents.get("output_capture_interval_sec", 1.0),
        memory_limit_mb=agents.get("memory_limit_mb", 2048),
        claude_command=claude_backend.get("command", "claude"),
        claude_args=claude_backend.get("args", ["--dangerously-skip-permissions"]),
        demo_mode=not has_claude,
    )


# ─────────────────────────────────────────────
# Section 3: Data Models
# ─────────────────────────────────────────────

@dataclass
class Role:
    key: str
    name: str
    icon: str
    color: str
    description: str
    system_prompt: str
    max_memory_mb: int = 2048

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "icon": self.icon,
            "color": self.color,
            "description": self.description,
            "system_prompt": self.system_prompt,
        }


BUILTIN_ROLES: dict[str, Role] = {
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


@dataclass
class Agent:
    id: str
    name: str
    role: str
    status: str  # spawning|planning|working|waiting|idle|error|paused
    working_dir: str
    backend: str
    task: str
    summary: str = ""
    context_pct: float = 0.0
    memory_mb: float = 0.0
    needs_input: bool = False
    input_prompt: str | None = None
    error_message: str | None = None
    project_id: str | None = None
    tmux_session: str = ""
    pid: int | None = None
    created_at: str = ""
    updated_at: str = ""
    script_path: str | None = None
    related_agents: list = field(default_factory=list)
    progress_pct: float = 0.0
    _phase: str = field(default="unknown", repr=False)
    output_lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    _prev_output_hash: int = field(default=0, repr=False)
    _total_chars: int = field(default=0, repr=False)
    _spawn_time: float = field(default=0.0, repr=False)
    _last_needs_input_event: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        role_obj = BUILTIN_ROLES.get(self.role)
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "role_icon": role_obj.icon if role_obj else "🤖",
            "role_color": role_obj.color if role_obj else "#64748B",
            "status": self.status,
            "working_dir": self.working_dir,
            "backend": self.backend,
            "task": self.task,
            "summary": self.summary,
            "context_pct": self.context_pct,
            "memory_mb": self.memory_mb,
            "needs_input": self.needs_input,
            "input_prompt": self.input_prompt,
            "error_message": self.error_message,
            "project_id": self.project_id,
            "pid": self.pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress_pct": self.progress_pct,
            "related_agents": self.related_agents,
        }

    def to_dict_full(self) -> dict:
        d = self.to_dict()
        d["output_lines"] = list(self.output_lines)
        return d


@dataclass
class SystemMetrics:
    cpu_pct: float = 0.0
    cpu_count: int = 0
    memory_total_gb: float = 0.0
    memory_used_gb: float = 0.0
    memory_available_gb: float = 0.0
    memory_pct: float = 0.0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_pct: float = 0.0
    load_avg: list = field(default_factory=list)
    agents_active: int = 0
    agents_total: int = 0

    def to_dict(self) -> dict:
        return {
            "cpu_pct": self.cpu_pct,
            "cpu_count": self.cpu_count,
            "memory": {
                "total_gb": self.memory_total_gb,
                "used_gb": self.memory_used_gb,
                "available_gb": self.memory_available_gb,
                "pct": self.memory_pct,
            },
            "disk": {
                "total_gb": self.disk_total_gb,
                "used_gb": self.disk_used_gb,
                "pct": self.disk_pct,
            },
            "load_avg": self.load_avg,
            "agents_active": self.agents_active,
            "agents_total": self.agents_total,
        }


# ─────────────────────────────────────────────
# Section 4: AgentManager — THE CORE
# ─────────────────────────────────────────────

class AgentManager:
    def __init__(self, config: Config):
        self.config = config
        self.agents: dict[str, Agent] = {}
        self.tmux_prefix = "ashlar"
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop

    # ── tmux helpers (run in executor to avoid blocking) ──

    async def _run_tmux(self, args: list[str], timeout: int = 5) -> subprocess.CompletedProcess:
        return await self.loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["tmux"] + args,
                capture_output=True, text=True, timeout=timeout
            )
        )

    async def _tmux_send_keys(self, session: str, text: str) -> bool:
        try:
            result = await self._run_tmux(["send-keys", "-t", session, text, "Enter"])
            return result.returncode == 0
        except Exception as e:
            log.error(f"tmux send-keys failed for {session}: {e}")
            return False

    async def _tmux_send_raw(self, session: str, key: str) -> bool:
        """Send a raw key (like C-c) without Enter."""
        try:
            result = await self._run_tmux(["send-keys", "-t", session, key])
            return result.returncode == 0
        except Exception as e:
            log.error(f"tmux send raw key failed for {session}: {e}")
            return False

    async def _tmux_session_exists(self, session: str) -> bool:
        try:
            result = await self._run_tmux(["has-session", "-t", session])
            return result.returncode == 0
        except Exception:
            return False

    async def _tmux_capture(self, session: str, lines: int = 200) -> list[str]:
        try:
            result = await self._run_tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
            if result.returncode == 0:
                return result.stdout.splitlines()
            return []
        except Exception:
            return []

    async def _tmux_get_pane_pid(self, session: str) -> int | None:
        try:
            result = await self._run_tmux(["list-panes", "-t", session, "-F", "#{pane_pid}"])
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split("\n")[0])
        except (ValueError, Exception):
            pass
        return None

    # ── Core operations ──

    async def spawn(
        self,
        role: str = "general",
        name: str | None = None,
        working_dir: str | None = None,
        task: str = "",
        plan_mode: bool = False,
        backend: str = "claude-code",
    ) -> Agent:
        """Spawn a new agent. Returns the Agent object."""
        if len(self.agents) >= self.config.max_agents:
            raise ValueError(f"Maximum agents ({self.config.max_agents}) reached")

        # Generate ID
        agent_id = uuid.uuid4().hex[:4]
        while agent_id in self.agents:
            agent_id = uuid.uuid4().hex[:4]

        # Generate name if not provided
        if not name:
            role_name = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"]).name.split()[0].lower()
            name = f"{role_name}-{agent_id}"

        # Resolve working directory
        if not working_dir:
            working_dir = self.config.default_working_dir
        working_dir = os.path.expanduser(working_dir)
        if not os.path.isdir(working_dir):
            os.makedirs(working_dir, exist_ok=True)

        session_name = f"{self.tmux_prefix}-{agent_id}"
        now = datetime.now(timezone.utc).isoformat()

        agent = Agent(
            id=agent_id,
            name=name,
            role=role,
            status="spawning",
            working_dir=working_dir,
            backend=backend if not self.config.demo_mode else "demo",
            task=task,
            summary="Starting up...",
            tmux_session=session_name,
            created_at=now,
            updated_at=now,
            _spawn_time=time.monotonic(),
        )
        self.agents[agent_id] = agent

        # Create tmux session
        try:
            result = await self._run_tmux([
                "new-session", "-d", "-s", session_name,
                "-x", "200", "-y", "50",
                "-c", working_dir,
            ])
            if result.returncode != 0:
                agent.status = "error"
                agent.error_message = f"Failed to create tmux session: {result.stderr}"
                log.error(f"tmux new-session failed: {result.stderr}")
                return agent
        except Exception as e:
            agent.status = "error"
            agent.error_message = str(e)
            return agent

        # Get pane PID
        agent.pid = await self._tmux_get_pane_pid(session_name)

        if self.config.demo_mode:
            # Demo mode: run a bash script that simulates agent behavior
            demo_script = self._build_demo_script(role, task, agent)
            await self._tmux_send_keys(session_name, demo_script)
        else:
            # Real mode: launch claude CLI
            cmd_parts = [self.config.claude_command] + self.config.claude_args
            cmd = " ".join(cmd_parts)
            await self._tmux_send_keys(session_name, cmd)

            # Wait for claude to start up
            await asyncio.sleep(3)

            # Send role system prompt as first message
            role_obj = BUILTIN_ROLES.get(role)
            if role_obj and role_obj.system_prompt and task:
                # Combine role context with task
                full_message = f"{role_obj.system_prompt}\n\nYour task: {task}"
                # Send each line separately for long messages
                for line in full_message.split("\n"):
                    if line.strip():
                        await self._tmux_send_keys(session_name, line)
                        await asyncio.sleep(0.1)
            elif task:
                await self._tmux_send_keys(session_name, task)

        agent.status = "working"
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        log.info(f"Spawned agent {agent_id} ({name}) with role {role}")
        return agent

    def _build_demo_script(self, role: str, task: str, agent: Agent | None = None) -> str:
        """Build a multi-phase bash script that simulates realistic agent behavior."""
        import random as _rand
        role_obj = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"])
        safe_task = task[:80].replace('"', '\\"').replace("'", "")

        # Role-specific working messages with file paths and progress
        role_work = {
            "security": [
                "Reading package.json for dependency audit...",
                "Scanning 142 dependencies for known CVEs...",
                "  ✓ No critical CVEs found in direct dependencies",
                "  ⚠ 3 moderate vulnerabilities in transitive deps",
                "Checking src/auth/login.ts for SQL injection...",
                "Auditing authentication flow in src/middleware/auth.ts...",
                "Found 2 potential XSS vectors in src/handlers/form.ts:47",
                "Reviewing CORS configuration in src/config/cors.ts...",
                "Checking for hardcoded secrets (scanning 89 files)...",
                "  ✓ No secrets detected in source files",
            ],
            "tester": [
                "Analyzing test coverage gaps across 23 modules...",
                "Writing unit tests for src/auth/login.test.ts...",
                "  ✓ Test: should reject invalid credentials (3ms)",
                "  ✓ Test: should rate-limit after 5 attempts (12ms)",
                "Running test suite: 12 passed, 0 failed (1.2s)",
                "Adding integration tests for POST /api/users...",
                "Testing edge cases for payment flow...",
                "  ✓ Test: handles currency conversion (8ms)",
                "  ✗ Test: timeout on webhook retry — needs fix",
                "Coverage report: 78% statements, 65% branches",
            ],
            "frontend": [
                "Reading component tree structure (47 components)...",
                "Analyzing responsive breakpoints in src/styles/...",
                "Editing src/components/Header.tsx (line 34-89)...",
                "  → Refactoring nav items to use flex layout",
                "Writing CSS module: src/components/Dashboard.module.css...",
                "Checking accessibility: adding aria labels to 12 elements...",
                "Editing src/components/Sidebar.tsx...",
                "  → Adding keyboard navigation support",
                "Optimizing bundle: removed 3 unused imports (-12KB)",
                "Running prettier on 8 modified files...",
            ],
            "backend": [
                "Reading database schema (14 tables, 67 columns)...",
                "Analyzing API endpoint patterns in src/routes/...",
                "Creating migration: 003_add_users_table.sql...",
                "  → Adding indexes on email and created_at",
                "Writing validation middleware in src/middleware/validate.ts...",
                "Adding rate limiting to POST /api/auth/* endpoints...",
                "Implementing cursor pagination for GET /api/items...",
                "  → Using created_at + id composite cursor",
                "Writing error handler for 4xx/5xx responses...",
                "Running linter: 0 errors, 2 warnings",
            ],
            "devops": [
                "Reading Dockerfile configuration...",
                "Analyzing CI/CD pipeline (4 stages, 12 jobs)...",
                "Optimizing Docker layer caching in build stage...",
                "  → Separating dependency install from source copy",
                "Configuring health check endpoint at /healthz...",
                "Setting up Prometheus alerting rules...",
                "  → CPU > 80% for 5m → PagerDuty",
                "Writing deployment rollback script: scripts/rollback.sh...",
                "Updating docker-compose.yml with resource limits...",
                "Validating k8s manifests with kubeval...",
            ],
            "architect": [
                "Analyzing system component boundaries (8 services)...",
                "Mapping data flow between auth → api → db...",
                "Evaluating caching strategies: Redis vs in-memory...",
                "  → Recommending Redis for session store (shared state)",
                "Designing event-driven communication pattern...",
                "  → Using NATS for inter-service messaging",
                "Creating sequence diagram for auth flow...",
                "Documenting API contract changes in docs/api-v2.md...",
                "Reviewing scalability: current bottleneck is DB writes...",
                "  → Recommending write-behind cache pattern",
            ],
        }

        work_msgs = role_work.get(role, [
            "Reading project structure (scanning files)...",
            "Analyzing codebase: 34 source files, 12K lines...",
            "Reading src/index.ts for entry point patterns...",
            "Writing implementation in src/features/new-feature.ts...",
            "  → Added 3 functions, 1 class",
            "Running linter checks (eslint)...",
            "  ✓ 0 errors, 0 warnings",
            "Verifying changes compile correctly (tsc --noEmit)...",
            "Checking for regressions in existing tests...",
            "  ✓ All 47 existing tests still pass",
        ])

        def rsleep(lo: float = 0.5, hi: float = 3.0) -> str:
            return f"sleep $(echo \"scale=1; {_rand.uniform(lo, hi):.1f}\" | bc)"

        # Build script lines
        script_lines = [
            '#!/bin/bash',
            f'echo "╭──────────────────────────────────────────╮"',
            f'echo "│ {role_obj.icon} Ashlar Agent (Demo Mode)            │"',
            f'echo "│ Role: {role_obj.name:<34}│"',
            f'echo "╰──────────────────────────────────────────╯"',
            f'echo ""',
            f'echo "Task: {safe_task}"',
            f'echo ""',
            # Phase 1: Planning (10-15s)
            'echo "Planning approach..."',
            rsleep(1.5, 3.0),
            'echo "Let me analyze the codebase structure first."',
            rsleep(1.0, 2.0),
            'echo ""',
            'echo "Here is my plan:"',
            'echo "  1. Read existing code and understand patterns"',
            'echo "  2. Implement the required changes"',
            'echo "  3. Write tests and verify correctness"',
            'echo "  4. Clean up and document"',
            rsleep(2.0, 4.0),
            'echo ""',
        ]

        # Phase 2: Working (30-60s total)
        for i, msg in enumerate(work_msgs):
            script_lines.append(f'echo "{msg}"')
            script_lines.append(rsleep(1.0, 4.0))

        # Phase 3: First question
        script_lines.extend([
            'echo ""',
            'echo "I have completed the initial implementation."',
            'echo "Do you want me to proceed with this approach? (yes/no)"',
            'read -r REPLY',
            'echo ""',
            'echo "Received: $REPLY"',
            'echo "Continuing with additional changes..."',
            rsleep(2.0, 4.0),
        ])

        # Phase 4: Second work phase
        second_phase = [
            "Writing additional test cases...",
            "  ✓ Added 4 edge case tests",
            "Updating documentation...",
            "Running final verification...",
        ]
        for msg in second_phase:
            script_lines.append(f'echo "{msg}"')
            script_lines.append(rsleep(1.5, 3.5))

        # Phase 5: Second question
        script_lines.extend([
            'echo ""',
            'echo "All changes are ready. Should I finalize and commit? (yes/no)"',
            'read -r REPLY',
            'echo ""',
            'echo "Received: $REPLY"',
            rsleep(1.0, 2.0),
            'echo "Finalizing changes..."',
            rsleep(2.0, 3.0),
            'echo "Done! Task completed successfully."',
            'sleep 86400',
        ])

        # Write to temp file and execute
        script_path = Path(tempfile.gettempdir()) / f"ashlar_demo_{uuid.uuid4().hex[:8]}.sh"
        script_path.write_text("\n".join(script_lines))
        script_path.chmod(0o755)
        if agent:
            agent.script_path = str(script_path)
        return f"bash {script_path}"

    async def kill(self, agent_id: str) -> bool:
        """Kill an agent gracefully. Returns the Agent before deletion for archival."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        session = agent.tmux_session
        log.info(f"Killing agent {agent_id} ({agent.name})")

        # Send /exit to claude
        try:
            await self._tmux_send_keys(session, "/exit")
            await asyncio.sleep(2)
        except Exception:
            pass

        # Force kill tmux session
        try:
            await self._run_tmux(["kill-session", "-t", session])
        except Exception:
            pass

        # Clean up demo script temp file
        if agent.script_path:
            try:
                Path(agent.script_path).unlink(missing_ok=True)
            except Exception:
                pass

        del self.agents[agent_id]
        return True

    async def pause(self, agent_id: str) -> bool:
        """Pause agent by sending Ctrl+C."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        await self._tmux_send_raw(agent.tmux_session, "C-c")
        agent.status = "paused"
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        log.info(f"Paused agent {agent_id}")
        return True

    async def resume(self, agent_id: str, message: str | None = None) -> bool:
        """Resume a paused agent."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        msg = message or agent.task or "continue"
        await self._tmux_send_keys(agent.tmux_session, msg)
        agent.status = "working"
        agent.needs_input = False
        agent.input_prompt = None
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        log.info(f"Resumed agent {agent_id}")
        return True

    async def send_message(self, agent_id: str, message: str) -> bool:
        """Send a message to an agent's tmux session."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        # Handle multi-line messages
        lines = message.split("\n")
        for line in lines:
            await self._tmux_send_keys(agent.tmux_session, line)
            if len(lines) > 1:
                await asyncio.sleep(0.1)

        agent.needs_input = False
        agent.input_prompt = None
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        return True

    async def capture_output(self, agent_id: str) -> list[str]:
        """Capture terminal output and return new lines since last capture."""
        agent = self.agents.get(agent_id)
        if not agent:
            return []

        raw_lines = await self._tmux_capture(agent.tmux_session)
        if not raw_lines:
            return []

        # Strip trailing empty lines
        while raw_lines and not raw_lines[-1].strip():
            raw_lines.pop()

        # Check if output changed
        output_hash = hash(tuple(raw_lines[-50:])) if raw_lines else 0
        if output_hash == agent._prev_output_hash:
            return []
        agent._prev_output_hash = output_hash

        # Find new lines by comparing with existing buffer
        existing = list(agent.output_lines)
        new_lines = []

        if not existing:
            new_lines = raw_lines
        else:
            # Find where old output ends in new output
            last_existing = existing[-1] if existing else ""
            found_idx = -1
            for i in range(len(raw_lines) - 1, -1, -1):
                if raw_lines[i] == last_existing:
                    found_idx = i
                    break
            if found_idx >= 0 and found_idx < len(raw_lines) - 1:
                new_lines = raw_lines[found_idx + 1:]
            elif found_idx < 0:
                # Couldn't match — treat all as new
                new_lines = raw_lines

        # Update ring buffer
        for line in raw_lines:
            agent.output_lines.append(line)

        # Track total chars for context estimation
        agent._total_chars += sum(len(l) for l in new_lines)
        # ~3.5 chars/token for English, 200K context window
        agent.context_pct = min(1.0, (agent._total_chars / 3.5) / 200000)

        return new_lines

    async def detect_status(self, agent_id: str) -> str:
        """Analyze recent output to detect agent's current status."""
        agent = self.agents.get(agent_id)
        if not agent:
            return "error"
        if agent.status == "paused":
            return "paused"

        recent = list(agent.output_lines)[-20:]
        if not recent:
            return agent.status

        return parse_agent_status(recent, agent)

    async def get_agent_memory(self, agent_id: str) -> float:
        """Get RSS memory of agent's process tree in MB."""
        agent = self.agents.get(agent_id)
        if not agent or not agent.pid:
            return 0.0

        try:
            proc = psutil.Process(agent.pid)
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return round(total / 1e6, 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    def cleanup_all(self) -> None:
        """Kill all ashlar tmux sessions and clean temp files. Synchronous for shutdown."""
        # Clean up temp demo scripts
        for agent in self.agents.values():
            if agent.script_path:
                try:
                    Path(agent.script_path).unlink(missing_ok=True)
                except Exception:
                    pass

        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for session_name in result.stdout.strip().split("\n"):
                    if session_name.startswith(self.tmux_prefix + "-"):
                        try:
                            subprocess.run(
                                ["tmux", "kill-session", "-t", session_name],
                                capture_output=True, timeout=5
                            )
                        except Exception:
                            pass
        except Exception:
            pass
        log.info("All agent sessions cleaned up")


# ─────────────────────────────────────────────
# Section 5: Status Parser
# ─────────────────────────────────────────────

STATUS_PATTERNS = {
    "planning": [
        re.compile(r"(?i)\bplan\b"),
        re.compile(r"(?i)let me (think|analyze|plan|consider)"),
        re.compile(r"(?i)here'?s (my|the) (plan|approach|strategy)"),
        re.compile(r"(?i)I'll (start by|first|begin)"),
        re.compile(r"(?i)thinking about"),
    ],
    "working": [
        re.compile(r"(?i)(writing|creating|editing|reading|updating) \S+\.\w+"),
        re.compile(r"(?i)(running|executing) .+"),
        re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"),
        re.compile(r"█+░*"),
        re.compile(r"(?i)Tool Use:"),
        re.compile(r"(?i)Bash:"),
        re.compile(r"(?i)files? (created|edited|read)"),
        re.compile(r"(?i)(scanning|checking|auditing|analyzing|testing)"),
    ],
    "waiting": [
        re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
        re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
        re.compile(r"(?i)proceed\?"),
        re.compile(r"(?i)\bapprove\b"),
    ],
    "error": [
        re.compile(r"(?i)\b(error|exception|traceback|failed|fatal)\b"),
        re.compile(r"(?i)command not found"),
        re.compile(r"(?i)permission denied"),
    ],
    "complete": [
        re.compile(r"(?i)\b(done|complete|finished|successfully)\b"),
        re.compile(r"(?i)task completed"),
        re.compile(r"(?i)all tests pass"),
    ],
}

WAITING_LINE_PATTERNS = [
    re.compile(r"\?\s*$"),
    re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
    re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
    re.compile(r"(?i)proceed\?"),
]


def parse_agent_status(recent_lines: list[str], agent: Agent) -> str:
    """Parse recent terminal output to detect agent status.
    Priority: waiting > error > planning > working > current status."""
    text_block = "\n".join(recent_lines)

    # Check for waiting (highest priority)
    for pattern in STATUS_PATTERNS["waiting"]:
        if pattern.search(text_block):
            # Extract the question
            agent.needs_input = True
            agent.input_prompt = _extract_question(recent_lines)
            return "waiting"

    # Check last non-empty line for question mark
    last_line = ""
    for line in reversed(recent_lines):
        if line.strip():
            last_line = line.strip()
            break

    for pattern in WAITING_LINE_PATTERNS:
        if pattern.search(last_line):
            agent.needs_input = True
            agent.input_prompt = last_line
            return "waiting"

    # Check for error
    for pattern in STATUS_PATTERNS["error"]:
        if pattern.search(text_block):
            # Only mark as error if it's recent (last 5 lines)
            error_text = "\n".join(recent_lines[-5:])
            if pattern.search(error_text):
                agent.needs_input = False
                return "error"

    # Check for planning
    for pattern in STATUS_PATTERNS["planning"]:
        if pattern.search(text_block):
            agent.needs_input = False
            return "planning"

    # Check for complete
    for pattern in STATUS_PATTERNS["complete"]:
        if pattern.search(text_block):
            # Only mark complete if in last 5 lines
            tail = "\n".join(recent_lines[-5:])
            if pattern.search(tail):
                agent.needs_input = False
                return "idle"

    # Check for working
    for pattern in STATUS_PATTERNS["working"]:
        if pattern.search(text_block):
            agent.needs_input = False
            return "working"

    # No clear signal — keep current status
    agent.needs_input = False
    return agent.status if agent.status != "spawning" else "working"


def _extract_question(lines: list[str]) -> str:
    """Extract the agent's question from recent output lines."""
    # Look backwards for question-like content
    question_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if question_lines:
                break
            continue
        question_lines.insert(0, stripped)
        if len(question_lines) >= 3:
            break
    return "\n".join(question_lines) if question_lines else "Agent needs your input"


_FILE_PATH_RE = re.compile(r"(?:(?:src|lib|test|app|pkg)/)?[\w\-./]+\.\w{1,5}")
_TEST_RESULT_RE = re.compile(r"(?i)(\d+)\s*(?:tests?\s+)?pass(?:ed)?.*?(\d+)\s*fail")
_COVERAGE_RE = re.compile(r"(?i)coverage[:\s]+(\d+)%")
_FILES_PROGRESS_RE = re.compile(r"(?i)(\d+)\s*(?:of|/)\s*(\d+)\s*(?:files?|items?)")

_ACTION_PATTERNS = [
    re.compile(r"(?i)(writing|creating|editing|reading|updating) (.+)"),
    re.compile(r"(?i)(running|executing) (.+)"),
    re.compile(r"(?i)(analyzing|reviewing|checking) (.+)"),
    re.compile(r"(?i)(installing|building|compiling) (.+)"),
    re.compile(r"(?i)(scanning|auditing|testing|deploying) (.+)"),
    re.compile(r"(?i)(found \d+.+)"),
    re.compile(r"(?i)(coverage.+\d+%)"),
]


def extract_summary(lines: list[str], task: str) -> str:
    """Extract a 1-2 line summary from recent output with file paths and test results."""
    # Check for test results
    for line in reversed(lines[-20:]):
        m = _TEST_RESULT_RE.search(line)
        if m:
            return f"Tests: {m.group(1)} passed, {m.group(2)} failed"
        m = _COVERAGE_RE.search(line)
        if m:
            return f"Coverage: {m.group(1)}%"

    # Extract file paths being worked on
    for line in reversed(lines[-20:]):
        stripped = line.strip()
        if not stripped:
            continue

        for pattern in _ACTION_PATTERNS:
            match = pattern.search(stripped)
            if match:
                # Try to extract file path from the match
                fp = _FILE_PATH_RE.search(stripped)
                if fp:
                    return f"{match.group(1).title()} {fp.group(0)}"[:100]
                return stripped[:100]

    # Files progress
    for line in reversed(lines[-15:]):
        m = _FILES_PROGRESS_RE.search(line)
        if m:
            return f"Progress: {m.group(1)} of {m.group(2)} files"

    # Fallback: last non-empty line
    for line in reversed(lines[-10:]):
        stripped = line.strip()
        if stripped and len(stripped) > 5:
            return stripped[:100]

    return task[:100] if task else "Working..."


# ── Phase detection for progress estimation ──

PHASE_PATTERNS = {
    "planning": [
        re.compile(r"(?i)(plan|approach|strategy|think|analyze|consider)"),
    ],
    "reading": [
        re.compile(r"(?i)(reading|scanning|exploring|listing) "),
    ],
    "implementing": [
        re.compile(r"(?i)(writing|creating|editing|modifying|adding|removing)"),
        re.compile(r"(?i)(implementing|building|refactoring)"),
    ],
    "testing": [
        re.compile(r"(?i)(running tests|test suite|coverage|verif)"),
        re.compile(r"(?i)(checking|linting|validating)"),
    ],
    "complete": [
        re.compile(r"(?i)\b(done|complete|finished|successfully)\b"),
    ],
}

PHASE_PROGRESS = {
    "unknown": 0.0,
    "planning": 0.10,
    "reading": 0.25,
    "implementing": 0.50,
    "testing": 0.80,
    "complete": 1.0,
}


def detect_phase(lines: list[str]) -> str:
    """Detect the current work phase from recent output."""
    recent = lines[-15:]
    text = "\n".join(recent)

    # Check from most advanced phase backwards
    for phase in ("complete", "testing", "implementing", "reading", "planning"):
        for pat in PHASE_PATTERNS[phase]:
            if pat.search(text):
                return phase
    return "unknown"


def estimate_progress(agent: Agent) -> float:
    """Estimate agent progress as 0.0–1.0."""
    phase = detect_phase(list(agent.output_lines))
    agent._phase = phase
    base = PHASE_PROGRESS.get(phase, 0.0)

    # Add time-based interpolation within the phase
    if agent.created_at:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(agent.created_at)).total_seconds()
            # Agents typically run 60–300s; add small time bonus
            time_bonus = min(0.10, elapsed / 600)
            return min(1.0, base + time_bonus)
        except Exception:
            pass
    return base


# ─────────────────────────────────────────────
# Section 5b: Database (SQLite Persistence)
# ─────────────────────────────────────────────

class Database:
    """Async SQLite layer for agent history, projects, and workflows."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (ASHLAR_DIR / "ashlar.db")
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS agents_history (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                project_id TEXT,
                task TEXT,
                summary TEXT,
                status TEXT,
                working_dir TEXT,
                backend TEXT,
                created_at TEXT,
                completed_at TEXT,
                duration_sec INTEGER,
                context_pct REAL,
                output_preview TEXT
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                agents_json TEXT NOT NULL,
                created_at TEXT
            );
        """)
        await self._db.commit()

        # Seed built-in workflows if empty
        async with self._db.execute("SELECT COUNT(*) FROM workflows") as cur:
            row = await cur.fetchone()
            if row[0] == 0:
                await self._seed_default_workflows()

        log.info(f"Database initialized at {self.db_path}")

    async def _seed_default_workflows(self) -> None:
        defaults = [
            {
                "id": "builtin-code-review",
                "name": "Code Review",
                "description": "Backend + Security + Reviewer agents for thorough code review",
                "agents_json": json.dumps([
                    {"role": "backend", "task": "Review the codebase for bugs and logic errors"},
                    {"role": "security", "task": "Audit for security vulnerabilities"},
                    {"role": "reviewer", "task": "Review code quality and suggest improvements"},
                ]),
            },
            {
                "id": "builtin-full-stack",
                "name": "Full Stack",
                "description": "Frontend + Backend + Tester for full-stack development",
                "agents_json": json.dumps([
                    {"role": "frontend", "task": "Build the frontend components"},
                    {"role": "backend", "task": "Build the API and backend logic"},
                    {"role": "tester", "task": "Write comprehensive tests"},
                ]),
            },
        ]
        for wf in defaults:
            await self._db.execute(
                "INSERT INTO workflows (id, name, description, agents_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (wf["id"], wf["name"], wf["description"], wf["agents_json"],
                 datetime.now(timezone.utc).isoformat()),
            )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Agent History ──

    async def save_agent(self, agent: Agent) -> None:
        completed_at = datetime.now(timezone.utc).isoformat()
        created = agent.created_at or completed_at
        try:
            created_dt = datetime.fromisoformat(created)
            completed_dt = datetime.fromisoformat(completed_at)
            duration = int((completed_dt - created_dt).total_seconds())
        except Exception:
            duration = 0

        output_preview = "\n".join(list(agent.output_lines)[-50:])

        await self._db.execute(
            """INSERT OR REPLACE INTO agents_history
               (id, name, role, project_id, task, summary, status, working_dir,
                backend, created_at, completed_at, duration_sec, context_pct, output_preview)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent.id, agent.name, agent.role, agent.project_id, agent.task,
             agent.summary, agent.status, agent.working_dir, agent.backend,
             agent.created_at, completed_at, duration, agent.context_pct, output_preview),
        )
        await self._db.commit()

    async def get_agent_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM agents_history ORDER BY completed_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_agent_history_item(self, agent_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM agents_history WHERE id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # ── Projects ──

    async def save_project(self, project: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO projects (id, name, path, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project["id"], project["name"], project["path"],
             project.get("description", ""), project.get("created_at", now), now),
        )
        await self._db.commit()

    async def get_projects(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM projects ORDER BY name") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def delete_project(self, project_id: str) -> bool:
        async with self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,)) as cur:
            await self._db.commit()
            return cur.rowcount > 0

    # ── Workflows ──

    async def save_workflow(self, workflow: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        agents_json = workflow.get("agents_json", "")
        if isinstance(agents_json, list):
            agents_json = json.dumps(agents_json)
        await self._db.execute(
            """INSERT OR REPLACE INTO workflows (id, name, description, agents_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (workflow["id"], workflow["name"], workflow.get("description", ""),
             agents_json, workflow.get("created_at", now)),
        )
        await self._db.commit()

    async def get_workflows(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM workflows ORDER BY name") as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["agents"] = json.loads(d.pop("agents_json", "[]"))
                except Exception:
                    d["agents"] = []
                result.append(d)
            return result

    async def get_workflow(self, workflow_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["agents"] = json.loads(d.pop("agents_json", "[]"))
            except Exception:
                d["agents"] = []
            return d

    async def delete_workflow(self, workflow_id: str) -> bool:
        async with self._db.execute(
            "DELETE FROM workflows WHERE id = ?", (workflow_id,)
        ) as cur:
            await self._db.commit()
            return cur.rowcount > 0


# ─────────────────────────────────────────────
# Section 6: Metrics Collector
# ─────────────────────────────────────────────

# Initialize CPU percent baseline
psutil.cpu_percent()


async def collect_system_metrics(agent_manager: AgentManager) -> SystemMetrics:
    """Collect system-wide metrics."""
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    active = sum(1 for a in agent_manager.agents.values() if a.status in ("working", "planning"))

    return SystemMetrics(
        cpu_pct=round(cpu, 1),
        cpu_count=psutil.cpu_count() or 1,
        memory_total_gb=round(mem.total / 1e9, 1),
        memory_used_gb=round(mem.used / 1e9, 1),
        memory_available_gb=round(mem.available / 1e9, 1),
        memory_pct=round(mem.percent, 1),
        disk_total_gb=round(disk.total / 1e9, 1),
        disk_used_gb=round(disk.used / 1e9, 1),
        disk_pct=round(disk.percent, 1),
        load_avg=[round(x, 2) for x in os.getloadavg()],
        agents_active=active,
        agents_total=len(agent_manager.agents),
    )


# ─────────────────────────────────────────────
# Section 7: WebSocket Hub
# ─────────────────────────────────────────────

class WebSocketHub:
    def __init__(self, agent_manager: AgentManager, config: Config, db: Database | None = None):
        self.clients: set[web.WebSocketResponse] = set()
        self.agent_manager = agent_manager
        self.config = config
        self.db = db

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self.clients.add(ws)
        log.info(f"WebSocket client connected ({len(self.clients)} total)")

        try:
            # Send full state sync
            projects = await self.db.get_projects() if self.db else []
            workflows = await self.db.get_workflows() if self.db else []
            await ws.send_json({
                "type": "sync",
                "agents": [a.to_dict() for a in self.agent_manager.agents.values()],
                "projects": projects,
                "workflows": workflows,
                "config": self.config.to_dict(),
            })

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self.handle_message(data, ws)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.error(f"WebSocket error: {ws.exception()}")
                    break
        except Exception as e:
            log.error(f"WebSocket handler error: {e}")
        finally:
            self.clients.discard(ws)
            log.info(f"WebSocket client disconnected ({len(self.clients)} total)")

        return ws

    async def handle_message(self, data: dict, ws: web.WebSocketResponse) -> None:
        msg_type = data.get("type")

        match msg_type:
            case "spawn":
                try:
                    agent = await self.agent_manager.spawn(
                        role=data.get("role", self.config.default_role),
                        name=data.get("name"),
                        working_dir=data.get("working_dir"),
                        task=data.get("task", ""),
                        plan_mode=data.get("plan_mode", False),
                        backend=data.get("backend", "claude-code"),
                    )
                    if data.get("project_id"):
                        agent.project_id = data["project_id"]
                    await self.broadcast({
                        "type": "agent_update",
                        "agent": agent.to_dict(),
                    })
                    await self.broadcast({
                        "type": "event",
                        "event": "agent_spawned",
                        "agent_id": agent.id,
                        "message": f"Agent {agent.name} spawned",
                    })
                except ValueError as e:
                    await ws.send_json({"type": "error", "message": str(e)})

            case "send":
                agent_id = data.get("agent_id")
                message = data.get("message", "")
                if agent_id and message:
                    await self.agent_manager.send_message(agent_id, message)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "kill":
                agent_id = data.get("agent_id")
                if agent_id:
                    agent = self.agent_manager.agents.get(agent_id)
                    name = agent.name if agent else "unknown"
                    # Archive to history before killing
                    if agent and self.db:
                        try:
                            await self.db.save_agent(agent)
                        except Exception as e:
                            log.warning(f"Failed to archive agent {agent_id}: {e}")
                    success = await self.agent_manager.kill(agent_id)
                    if success:
                        await self.broadcast({
                            "type": "agent_removed",
                            "agent_id": agent_id,
                        })
                        await self.broadcast({
                            "type": "event",
                            "event": "agent_killed",
                            "agent_id": agent_id,
                            "message": f"Agent {name} killed",
                        })

            case "pause":
                agent_id = data.get("agent_id")
                if agent_id:
                    await self.agent_manager.pause(agent_id)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "resume":
                agent_id = data.get("agent_id")
                message = data.get("message")
                if agent_id:
                    await self.agent_manager.resume(agent_id, message)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "sync_request":
                projects = await self.db.get_projects() if self.db else []
                workflows = await self.db.get_workflows() if self.db else []
                await ws.send_json({
                    "type": "sync",
                    "agents": [a.to_dict() for a in self.agent_manager.agents.values()],
                    "projects": projects,
                    "workflows": workflows,
                    "config": self.config.to_dict(),
                })

            case _:
                await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        dead: set[web.WebSocketResponse] = set()
        for ws in self.clients:
            try:
                await ws.send_json(message)
            except (ConnectionError, RuntimeError, ConnectionResetError):
                dead.add(ws)
        self.clients -= dead


# ─────────────────────────────────────────────
# Section 8: REST API Handlers
# ─────────────────────────────────────────────

async def serve_dashboard(request: web.Request) -> web.FileResponse:
    dashboard_path = Path(__file__).parent / "ashlar_dashboard.html"
    if not dashboard_path.exists():
        return web.Response(text="Dashboard not found. Create ashlar_dashboard.html.", status=404)
    return web.FileResponse(dashboard_path)


async def serve_logo(request: web.Request) -> web.Response:
    logo_path = Path(__file__).parent / "White Ashlar logo copy.png"
    if not logo_path.exists():
        return web.Response(text="Logo not found", status=404)
    return web.FileResponse(logo_path, headers={"Cache-Control": "public, max-age=86400"})


async def list_agents(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agents = [a.to_dict() for a in manager.agents.values()]
    return web.json_response(agents)


async def spawn_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        agent = await manager.spawn(
            role=data.get("role", request.app["config"].default_role),
            name=data.get("name"),
            working_dir=data.get("working_dir"),
            task=data.get("task", ""),
            plan_mode=data.get("plan_mode", False),
            backend=data.get("backend", "claude-code"),
        )

        # Broadcast to WebSocket clients
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        await hub.broadcast({
            "type": "event",
            "event": "agent_spawned",
            "agent_id": agent.id,
            "message": f"Agent {agent.name} spawned",
        })

        return web.json_response(agent.to_dict(), status=201)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=503)


async def get_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    return web.json_response(agent.to_dict_full())


async def delete_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    # Archive to history before killing
    try:
        await db.save_agent(agent)
    except Exception as e:
        log.warning(f"Failed to archive agent {agent_id}: {e}")

    name = agent.name
    success = await manager.kill(agent_id)
    if success:
        await hub.broadcast({
            "type": "agent_removed",
            "agent_id": agent_id,
        })
        await hub.broadcast({
            "type": "event",
            "event": "agent_killed",
            "agent_id": agent_id,
            "message": f"Agent {name} killed",
        })
        return web.json_response({"status": "killed"})
    return web.json_response({"error": "Failed to kill agent"}, status=500)


async def send_to_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    message = data.get("message", "")
    if not message:
        return web.json_response({"error": "No message provided"}, status=400)

    success = await manager.send_message(agent_id, message)
    if success:
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "sent"})
    return web.json_response({"error": "Failed to send message"}, status=500)


async def pause_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    success = await manager.pause(agent_id)
    if success:
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "paused"})
    return web.json_response({"error": "Failed to pause"}, status=500)


async def resume_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        data = await request.json()
        message = data.get("message")
    except Exception:
        message = None

    success = await manager.resume(agent_id, message)
    if success:
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "resumed"})
    return web.json_response({"error": "Failed to resume"}, status=500)


async def system_metrics(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    metrics = await collect_system_metrics(manager)
    return web.json_response(metrics.to_dict())


async def list_roles(request: web.Request) -> web.Response:
    roles = {k: v.to_dict() for k, v in BUILTIN_ROLES.items()}
    return web.json_response(roles)


async def get_agent_output(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    offset = int(request.query.get("offset", 0))
    limit = int(request.query.get("limit", 200))
    all_lines = list(agent.output_lines)
    total = len(all_lines)
    sliced = all_lines[offset:offset + limit]

    return web.json_response({
        "lines": sliced,
        "total": total,
        "offset": offset,
    })


async def get_config(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    return web.json_response(config.to_dict())


async def put_config(request: web.Request) -> web.Response:
    """Update runtime config and save to ashlar.yaml."""
    config: Config = request.app["config"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Update in-memory config
    for key in ("max_agents", "default_role", "default_working_dir",
                "output_capture_interval", "memory_limit_mb"):
        if key in data:
            if hasattr(config, key):
                setattr(config, key, data[key])

    # Save to yaml
    config_path = ASHLAR_DIR / "ashlar.yaml"
    try:
        raw = DEFAULT_CONFIG.copy()
        if config_path.exists():
            with open(config_path) as f:
                raw = deep_merge(raw, yaml.safe_load(f) or {})
        raw = deep_merge(raw, data)
        with open(config_path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        log.warning(f"Failed to save config: {e}")

    return web.json_response(config.to_dict())


# ── Project endpoints ──

async def list_projects(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    projects = await db.get_projects()
    return web.json_response(projects)


async def create_project(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data.get("name") or not data.get("path"):
        return web.json_response({"error": "name and path are required"}, status=400)

    project = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"],
        "path": os.path.expanduser(data["path"]),
        "description": data.get("description", ""),
    }
    await db.save_project(project)
    return web.json_response(project, status=201)


async def delete_project(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    success = await db.delete_project(project_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Project not found"}, status=404)


# ── Workflow endpoints ──

async def list_workflows(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflows = await db.get_workflows()
    return web.json_response(workflows)


async def create_workflow(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data.get("name") or not data.get("agents"):
        return web.json_response({"error": "name and agents are required"}, status=400)

    workflow = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"],
        "description": data.get("description", ""),
        "agents_json": data["agents"],
    }
    await db.save_workflow(workflow)
    workflow["agents"] = data["agents"]
    return web.json_response(workflow, status=201)


async def run_workflow(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    workflow_id = request.match_info["id"]

    workflow = await db.get_workflow(workflow_id)
    if not workflow:
        return web.json_response({"error": "Workflow not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    working_dir = body.get("working_dir")

    agent_ids = []
    related = []
    for agent_def in workflow.get("agents", []):
        try:
            agent = await manager.spawn(
                role=agent_def.get("role", "general"),
                name=agent_def.get("name"),
                working_dir=agent_def.get("working_dir") or working_dir,
                task=agent_def.get("task", ""),
            )
            agent_ids.append(agent.id)
            related.append(agent.id)
        except ValueError as e:
            log.warning(f"Workflow spawn failed: {e}")

    # Link related agents
    for aid in agent_ids:
        a = manager.agents.get(aid)
        if a:
            a.related_agents = [x for x in related if x != aid]
            await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})

    await hub.broadcast({
        "type": "event",
        "event": "workflow_started",
        "message": f"Workflow '{workflow['name']}' started ({len(agent_ids)} agents)",
    })

    return web.json_response({"agent_ids": agent_ids, "workflow": workflow["name"]})


async def delete_workflow(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflow_id = request.match_info["id"]
    if workflow_id.startswith("builtin-"):
        return web.json_response({"error": "Cannot delete built-in workflows"}, status=400)
    success = await db.delete_workflow(workflow_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Workflow not found"}, status=404)


# ── History endpoints ──

async def list_history(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    limit = int(request.query.get("limit", 50))
    offset = int(request.query.get("offset", 0))
    history = await db.get_agent_history(limit, offset)
    return web.json_response(history)


async def get_history_item(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    item = await db.get_agent_history_item(agent_id)
    if not item:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(item)


# ─────────────────────────────────────────────
# Section 9: Background Tasks
# ─────────────────────────────────────────────

async def output_capture_loop(app: web.Application) -> None:
    """Capture output from all agents every ~1 second."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    interval = app["config"].output_capture_interval

    while True:
        try:
            for agent_id, agent in list(manager.agents.items()):
                if agent.status in ("paused",):
                    continue

                # Spawn timeout: 30s with no output → error
                if agent.status == "spawning" and agent._spawn_time > 0:
                    if time.monotonic() - agent._spawn_time > 30:
                        agent.status = "error"
                        agent.error_message = "Spawn timeout — no output after 30s"
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast({
                            "type": "event",
                            "event": "agent_error",
                            "agent_id": agent_id,
                            "message": f"Agent {agent.name} spawn timed out",
                        })
                        continue

                # Capture output
                try:
                    new_lines = await manager.capture_output(agent_id)
                    if new_lines:
                        await hub.broadcast({
                            "type": "agent_output",
                            "agent_id": agent_id,
                            "lines": new_lines,
                        })

                        # Update summary and progress
                        agent.summary = extract_summary(list(agent.output_lines), agent.task)
                        agent.progress_pct = estimate_progress(agent)
                except Exception as e:
                    log.debug(f"Output capture error for {agent_id}: {e}")

                # Detect status
                try:
                    new_status = await manager.detect_status(agent_id)
                    if new_status != agent.status:
                        old_status = agent.status
                        agent.status = new_status
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        log.debug(f"Agent {agent_id} status: {old_status} -> {new_status}")

                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

                        if agent.needs_input:
                            # Debounce: suppress duplicate needs_input events within 5s
                            now_mono = time.monotonic()
                            if now_mono - agent._last_needs_input_event > 5.0:
                                agent._last_needs_input_event = now_mono
                                await hub.broadcast({
                                    "type": "event",
                                    "event": "agent_needs_input",
                                    "agent_id": agent_id,
                                    "message": agent.input_prompt or "Agent needs input",
                                })
                    else:
                        # Broadcast updates even if status unchanged (summary/context may have changed)
                        if new_lines:
                            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                except Exception as e:
                    log.debug(f"Status detection error for {agent_id}: {e}")

        except Exception as e:
            log.error(f"Output capture loop error: {e}")

        await asyncio.sleep(interval)


async def metrics_loop(app: web.Application) -> None:
    """Collect and broadcast system metrics every 2 seconds."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]

    while True:
        try:
            metrics = await collect_system_metrics(manager)

            # Also update per-agent memory
            for agent_id, agent in list(manager.agents.items()):
                try:
                    agent.memory_mb = await manager.get_agent_memory(agent_id)
                except Exception:
                    pass

            await hub.broadcast({"type": "metrics", **metrics.to_dict()})
        except Exception as e:
            log.error(f"Metrics loop error: {e}")

        await asyncio.sleep(2.0)


async def health_check_loop(app: web.Application) -> None:
    """Verify tmux sessions are alive, clean up dead agents."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]

    while True:
        try:
            for agent_id, agent in list(manager.agents.items()):
                if agent.status == "error":
                    continue

                exists = await manager._tmux_session_exists(agent.tmux_session)
                if not exists:
                    log.warning(f"Agent {agent_id} ({agent.name}) tmux session died")
                    agent.status = "error"
                    agent.error_message = "tmux session terminated unexpectedly"
                    agent.updated_at = datetime.now(timezone.utc).isoformat()
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast({
                        "type": "event",
                        "event": "agent_error",
                        "agent_id": agent_id,
                        "message": f"Agent {agent.name} died unexpectedly",
                    })
        except Exception as e:
            log.error(f"Health check error: {e}")

        await asyncio.sleep(5.0)


async def memory_watchdog_loop(app: web.Application) -> None:
    """Check per-agent memory usage, warn/kill if over limit."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    limit = app["config"].memory_limit_mb
    warn_threshold = limit * 0.75

    while True:
        try:
            for agent_id, agent in list(manager.agents.items()):
                if agent.memory_mb > limit:
                    log.warning(f"Agent {agent_id} exceeded memory limit ({agent.memory_mb}MB > {limit}MB), killing")
                    name = agent.name
                    await manager.kill(agent_id)
                    await hub.broadcast({
                        "type": "agent_removed",
                        "agent_id": agent_id,
                    })
                    await hub.broadcast({
                        "type": "event",
                        "event": "agent_killed",
                        "agent_id": agent_id,
                        "message": f"Agent {name} killed: memory limit exceeded ({agent.memory_mb}MB)",
                    })
                elif agent.memory_mb > warn_threshold:
                    log.warning(f"Agent {agent_id} memory warning: {agent.memory_mb}MB / {limit}MB")
        except Exception as e:
            log.error(f"Memory watchdog error: {e}")

        await asyncio.sleep(10.0)


# ─────────────────────────────────────────────
# Section 10: Application Setup & Main
# ─────────────────────────────────────────────

async def start_background_tasks(app: web.Application) -> None:
    # Initialize database
    db: Database = app["db"]
    await db.init()

    app["bg_tasks"] = [
        asyncio.create_task(output_capture_loop(app)),
        asyncio.create_task(metrics_loop(app)),
        asyncio.create_task(health_check_loop(app)),
        asyncio.create_task(memory_watchdog_loop(app)),
    ]
    log.info("Background tasks started")


async def cleanup_background_tasks(app: web.Application) -> None:
    for task in app.get("bg_tasks", []):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Archive remaining agents to history
    db: Database = app["db"]
    manager: AgentManager = app["agent_manager"]
    for agent in manager.agents.values():
        try:
            await db.save_agent(agent)
        except Exception:
            pass

    # Close database
    await db.close()

    # Clean up all tmux sessions
    manager.cleanup_all()
    log.info("Cleanup complete")


def create_app(config: Config) -> web.Application:
    app = web.Application()
    app["config"] = config

    manager = AgentManager(config)
    app["agent_manager"] = manager

    db = Database()
    app["db"] = db

    hub = WebSocketHub(manager, config, db)
    app["ws_hub"] = hub

    # Routes
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/logo.png", serve_logo)
    app.router.add_get("/ws", hub.handle_ws)

    # REST API — Agents
    app.router.add_get("/api/agents", list_agents)
    app.router.add_post("/api/agents", spawn_agent)
    app.router.add_get("/api/agents/{id}", get_agent)
    app.router.add_delete("/api/agents/{id}", delete_agent)
    app.router.add_post("/api/agents/{id}/send", send_to_agent)
    app.router.add_post("/api/agents/{id}/pause", pause_agent)
    app.router.add_post("/api/agents/{id}/resume", resume_agent)
    app.router.add_get("/api/agents/{id}/output", get_agent_output)

    # REST API — System
    app.router.add_get("/api/system", system_metrics)
    app.router.add_get("/api/roles", list_roles)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)

    # REST API — Projects
    app.router.add_get("/api/projects", list_projects)
    app.router.add_post("/api/projects", create_project)
    app.router.add_delete("/api/projects/{id}", delete_project)

    # REST API — Workflows
    app.router.add_get("/api/workflows", list_workflows)
    app.router.add_post("/api/workflows", create_workflow)
    app.router.add_post("/api/workflows/{id}/run", run_workflow)
    app.router.add_delete("/api/workflows/{id}", delete_workflow)

    # REST API — History
    app.router.add_get("/api/history", list_history)
    app.router.add_get("/api/history/{id}", get_history_item)

    # CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })
    for route in list(app.router.routes()):
        try:
            cors.add(route)
        except ValueError:
            pass

    # Background tasks
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app


def setup_signal_handlers(agent_manager: AgentManager) -> None:
    def handle_shutdown(signum: int, frame: Any) -> None:
        print("\n\033[33m→ Shutting down Ashlar...\033[0m")
        agent_manager.cleanup_all()
        print("\033[32m✓ All agent sessions cleaned up\033[0m")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)


def main() -> None:
    print_banner()
    has_claude = check_dependencies()
    config = load_config(has_claude)
    setup_logging(config.log_level)

    app = create_app(config)

    # Signal handlers need the agent manager reference
    setup_signal_handlers(app["agent_manager"])

    mode_str = "\033[33mDEMO MODE\033[0m" if config.demo_mode else "\033[32mLIVE MODE\033[0m"
    print(f"  Mode: {mode_str}")
    print(f"  Dashboard: \033[36mhttp://{config.host}:{config.port}\033[0m")
    print(f"  Max agents: {config.max_agents}")
    print()

    web.run_app(app, host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
