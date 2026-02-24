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

# ── Module-level ANSI stripping utility ──

_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    return _ANSI_ESCAPE_RE.sub('', text)


# ── Secret Redaction ──

_SECRET_PATTERNS = [
    re.compile(r'\b(sk-[a-zA-Z0-9]{20,})'),           # OpenAI/Anthropic API keys
    re.compile(r'\b(ghp_[a-zA-Z0-9]{36,})'),           # GitHub PATs
    re.compile(r'\b(xai-[a-zA-Z0-9]{20,})'),           # xAI API keys
    re.compile(r'\b(AKIA[A-Z0-9]{16})'),               # AWS access keys
    re.compile(r'\b(Bearer\s+[a-zA-Z0-9\-._~+/]{20,})'),  # Bearer tokens
    re.compile(r'(?i)\bpassword\s*[=:]\s*\S+'),        # password= fields
    re.compile(r'\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),  # JWT tokens
]


def redact_secrets(text: str) -> str:
    """Replace secret patterns with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub('****[REDACTED]', result)
    return result


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
    "server": {"host": "127.0.0.1", "port": 5000, "log_level": "INFO", "require_auth": False, "auth_token": ""},
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
    "llm": {
        "enabled": False,
        "provider": "xai",
        "model": "grok-4-1-fast-reasoning",
        "api_key_env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "summary_interval_sec": 10.0,
        "max_output_lines": 30,
    },
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
    # Auth
    require_auth: bool = False
    auth_token: str = ""
    # Multi-backend support
    backends: dict = field(default_factory=lambda: {
        "claude-code": {"command": "claude", "args": ["--dangerously-skip-permissions"]},
        "codex": {"command": "codex", "args": []},
    })
    default_backend: str = "claude-code"
    # Voice
    voice_feedback: bool = True
    # Idle agent reaping
    idle_agent_ttl: int = 3600  # seconds before idle/complete agents are reaped
    # LLM summary config
    llm_enabled: bool = False
    llm_provider: str = "xai"
    llm_model: str = "grok-4-1-fast-reasoning"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.x.ai/v1"
    llm_summary_interval: float = 10.0
    llm_max_output_lines: int = 30
    # Configurable alert thresholds
    health_low_threshold: float = 0.3
    health_critical_threshold: float = 0.1
    stall_timeout_minutes: int = 5
    hung_timeout_minutes: int = 10

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
            "default_backend": self.default_backend,
            "backends": {k: {"command": v.get("command", ""), "available": bool(shutil.which(v.get("command", "")))} for k, v in self.backends.items() if isinstance(v, dict)},
            "llm_enabled": self.llm_enabled,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_summary_interval": self.llm_summary_interval,
            "voice_feedback": self.voice_feedback,
            "idle_agent_ttl": self.idle_agent_ttl,
            "health_low_threshold": self.health_low_threshold,
            "health_critical_threshold": self.health_critical_threshold,
            "stall_timeout_minutes": self.stall_timeout_minutes,
            "hung_timeout_minutes": self.hung_timeout_minutes,
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
    voice = raw.get("voice", {})
    llm = raw.get("llm", {})

    default_wd = agents.get("default_working_dir", "~/Projects")
    default_wd = os.path.expanduser(default_wd)

    # Validate config values — warn and use defaults for invalid entries
    def _validate(value, validator, default, name):
        if validator(value):
            return value
        log.warning(f"Invalid config value for {name}: {value!r}, using default: {default!r}")
        return default

    alerts = raw.get("alerts", {})
    max_agents_val = agents.get("max_concurrent", 16)
    max_agents_val = _validate(max_agents_val, lambda v: isinstance(v, int) and 1 <= v <= 100, 16, "max_concurrent")
    output_interval_val = agents.get("output_capture_interval_sec", 1.0)
    output_interval_val = _validate(output_interval_val, lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 30.0, 1.0, "output_capture_interval_sec")
    memory_limit_val = agents.get("memory_limit_mb", 2048)
    memory_limit_val = _validate(memory_limit_val, lambda v: isinstance(v, int) and 256 <= v <= 32768, 2048, "memory_limit_mb")
    idle_ttl_val = agents.get("idle_agent_ttl", alerts.get("idle_ttl_seconds", 3600))
    idle_ttl_val = _validate(idle_ttl_val, lambda v: isinstance(v, int) and 300 <= v <= 86400, 3600, "idle_agent_ttl")
    llm_interval_val = llm.get("summary_interval_sec", 10.0)
    llm_interval_val = _validate(llm_interval_val, lambda v: isinstance(v, (int, float)) and 3.0 <= v <= 120.0, 10.0, "llm_summary_interval")
    health_low_val = alerts.get("health_low_threshold", 0.3)
    health_low_val = _validate(health_low_val, lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0, 0.3, "health_low_threshold")
    health_crit_val = alerts.get("health_critical_threshold", 0.1)
    health_crit_val = _validate(health_crit_val, lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0, 0.1, "health_critical_threshold")
    stall_val = alerts.get("stall_timeout_minutes", 5)
    stall_val = _validate(stall_val, lambda v: isinstance(v, int) and 1 <= v <= 60, 5, "stall_timeout_minutes")
    hung_val = alerts.get("hung_timeout_minutes", 10)
    hung_val = _validate(hung_val, lambda v: isinstance(v, int) and 1 <= v <= 120, 10, "hung_timeout_minutes")

    # Resolve LLM API key from env var
    api_key_env = llm.get("api_key_env", "XAI_API_KEY")
    llm_api_key = os.environ.get(api_key_env, "")

    # Auth config
    require_auth = server.get("require_auth", False)
    auth_token = server.get("auth_token", "")
    if require_auth and not auth_token:
        # Auto-generate a 24-char token
        import secrets as _secrets
        auth_token = _secrets.token_urlsafe(18)
        log.info(f"Auto-generated auth token: {auth_token}")
        # Save it back to config
        try:
            if config_path.exists():
                with open(config_path) as f:
                    raw_yaml = yaml.safe_load(f) or {}
            else:
                raw_yaml = {}
            raw_yaml.setdefault("server", {})["auth_token"] = auth_token
            with open(config_path, "w") as f:
                yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass

    return Config(
        host=server.get("host", "127.0.0.1"),
        port=server.get("port", 5000),
        log_level=server.get("log_level", "INFO"),
        max_agents=max_agents_val,
        default_role=agents.get("default_role", "general"),
        default_working_dir=default_wd,
        output_capture_interval=output_interval_val,
        memory_limit_mb=memory_limit_val,
        claude_command=claude_backend.get("command", "claude"),
        claude_args=claude_backend.get("args", ["--dangerously-skip-permissions"]),
        demo_mode=not has_claude,
        voice_feedback=voice.get("feedback_sounds", True),
        require_auth=require_auth,
        auth_token=auth_token,
        backends=backends or DEFAULT_CONFIG["agents"]["backends"],
        default_backend=agents.get("default_backend", "claude-code"),
        llm_enabled=llm.get("enabled", False) and bool(llm_api_key),
        llm_provider=llm.get("provider", "xai"),
        llm_model=llm.get("model", "grok-4-1-fast-reasoning"),
        llm_api_key=llm_api_key,
        llm_base_url=llm.get("base_url", "https://api.x.ai/v1"),
        llm_summary_interval=llm_interval_val,
        llm_max_output_lines=llm.get("max_output_lines", 30),
        idle_agent_ttl=idle_ttl_val,
        health_low_threshold=health_low_val,
        health_critical_threshold=health_crit_val,
        stall_timeout_minutes=stall_val,
        hung_timeout_minutes=hung_val,
    )


# ─────────────────────────────────────────────
# Section 3: Data Models
# ─────────────────────────────────────────────

@dataclass
class BackendConfig:
    """Rich configuration for an agent backend CLI tool."""
    command: str
    args: list[str] = field(default_factory=list)
    available: bool = False
    # Orchestration capabilities
    supports_json_output: bool = False
    supports_system_prompt: bool = False
    supports_tool_restriction: bool = False
    supports_session_resume: bool = False
    supports_model_select: bool = False
    # Automation
    auto_approve_flag: str = ""
    # Status detection overrides (merge with defaults)
    status_patterns: dict[str, list[str]] = field(default_factory=dict)
    # Cost rates (per 1K tokens)
    cost_input_per_1k: float = 0.003
    cost_output_per_1k: float = 0.015

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "args": self.args,
            "available": self.available,
            "supports_json_output": self.supports_json_output,
            "supports_system_prompt": self.supports_system_prompt,
            "supports_tool_restriction": self.supports_tool_restriction,
            "supports_session_resume": self.supports_session_resume,
            "supports_model_select": self.supports_model_select,
            "auto_approve_flag": self.auto_approve_flag,
            "cost_input_per_1k": self.cost_input_per_1k,
            "cost_output_per_1k": self.cost_output_per_1k,
        }


KNOWN_BACKENDS: dict[str, BackendConfig] = {
    "claude-code": BackendConfig(
        command="claude",
        args=["--dangerously-skip-permissions"],
        supports_json_output=True,
        supports_system_prompt=True,
        supports_tool_restriction=True,
        supports_session_resume=True,
        supports_model_select=True,
        auto_approve_flag="--dangerously-skip-permissions",
        status_patterns={
            "working": [r"⎿", r"Tool Use:", r"Bash:"],
            "waiting": [r"Do you want to proceed", r"Allow once", r"Allow always"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
    ),
    "codex": BackendConfig(
        command="codex",
        args=[],
        supports_json_output=True,
        auto_approve_flag="--full-auto",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.012,
    ),
    "aider": BackendConfig(
        command="aider",
        args=[],
        supports_model_select=True,
        auto_approve_flag="-y",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
    ),
    "goose": BackendConfig(
        command="goose",
        args=[],
        supports_session_resume=True,
        auto_approve_flag="-y",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
    ),
}


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
    status: str  # spawning|planning|reading|working|waiting|idle|error|paused
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
    phase: str = ""
    # Auto-restart fields
    restart_count: int = 0
    max_restarts: int = 3
    last_restart_time: float = 0.0
    restarted_at: str = ""
    _restart_in_progress: bool = field(default=False, repr=False)
    # Health scoring fields
    health_score: float = 1.0
    error_count: int = 0
    last_output_time: float = 0.0
    # Per-agent metrics
    time_to_first_output: float = 0.0  # seconds from spawn to first output
    total_output_lines: int = 0
    output_rate: float = 0.0  # lines per minute, rolling average
    _phase: str = field(default="unknown", repr=False)
    output_lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=2000))
    _archived_lines: int = field(default=0, repr=False)  # count of lines archived to SQLite
    _prev_output_hash: int = field(default=0, repr=False)
    _total_chars: int = field(default=0, repr=False)
    _spawn_time: float = field(default=0.0, repr=False)
    _last_needs_input_event: float = field(default=0.0, repr=False)
    _last_llm_summary_time: float = field(default=0.0, repr=False)
    _llm_summary: str = field(default="", repr=False)
    # Orchestration fields
    model: str | None = None
    tools_allowed: list[str] | None = None
    session_id: str | None = None
    system_prompt: str | None = None
    workflow_run_id: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    estimated_cost_usd: float = 0.0
    files_touched: int = 0
    unread_messages: int = field(default=0, repr=False)
    _first_output_received: bool = field(default=False, repr=False)
    _output_line_timestamps: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=60), repr=False
    )  # timestamps of recent output batches for rate calculation
    _error_entered_at: float = field(default=0.0, repr=False)  # monotonic time when error status was first detected
    _health_low_warned: bool = field(default=False, repr=False)
    _health_critical_warned: bool = field(default=False, repr=False)
    _workflow_stall_warned: bool = field(default=False, repr=False)
    _workflow_hung_warned: bool = field(default=False, repr=False)

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
            "phase": self.phase,
            "related_agents": self.related_agents,
            "unread_messages": self.unread_messages,
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
            "restarted_at": self.restarted_at,
            "health_score": round(self.health_score, 3),
            "error_count": self.error_count,
            "time_to_first_output": round(self.time_to_first_output, 2),
            "total_output_lines": self.total_output_lines,
            "output_rate": round(self.output_rate, 1),
            "files_touched": self.files_touched,
            "model": self.model,
            "tools_allowed": self.tools_allowed,
            "workflow_run_id": self.workflow_run_id,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "cost_is_estimated": True,
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

@dataclass
class WorkflowRun:
    """Tracks a running workflow pipeline with dependency management."""
    id: str
    workflow_id: str
    workflow_name: str
    agent_specs: list[dict]
    agent_map: dict[int, str] = field(default_factory=dict)  # spec_index → agent_id
    pending_indices: set[int] = field(default_factory=set)
    running_ids: set[str] = field(default_factory=set)
    completed_ids: set[str] = field(default_factory=set)
    failed_ids: set[str] = field(default_factory=set)
    status: str = "running"  # running|completed|failed|cancelled
    working_dir: str = ""
    created_at: str = ""
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "agent_specs": self.agent_specs,
            "agent_map": {str(k): v for k, v in self.agent_map.items()},
            "pending_indices": list(self.pending_indices),
            "running_ids": list(self.running_ids),
            "completed_ids": list(self.completed_ids),
            "failed_ids": list(self.failed_ids),
            "status": self.status,
            "working_dir": self.working_dir,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class AgentManager:
    def __init__(self, config: Config):
        self.config = config
        self.agents: dict[str, Agent] = {}
        self.tmux_prefix = "ashlar"
        self._loop: asyncio.AbstractEventLoop | None = None
        # Rich backend configs
        self.backend_configs: dict[str, BackendConfig] = self._build_backend_configs()
        # Workflow run tracking
        self.workflow_runs: dict[str, WorkflowRun] = {}
        # File activity tracking for conflict detection
        self.file_activity: dict[str, dict[str, str]] = {}  # file_path → {agent_id: operation}

    def _build_backend_configs(self) -> dict[str, BackendConfig]:
        """Build BackendConfig objects from known defaults + user config."""
        configs: dict[str, BackendConfig] = {}
        # Start with known defaults
        for name, bc in KNOWN_BACKENDS.items():
            configs[name] = BackendConfig(
                command=bc.command, args=list(bc.args), available=False,
                supports_json_output=bc.supports_json_output,
                supports_system_prompt=bc.supports_system_prompt,
                supports_tool_restriction=bc.supports_tool_restriction,
                supports_session_resume=bc.supports_session_resume,
                supports_model_select=bc.supports_model_select,
                auto_approve_flag=bc.auto_approve_flag,
                cost_input_per_1k=bc.cost_input_per_1k,
                cost_output_per_1k=bc.cost_output_per_1k,
            )
        # Override with user config
        for name, cfg in self.config.backends.items():
            if name in configs:
                configs[name].command = cfg.get("command", configs[name].command)
                configs[name].args = cfg.get("args", configs[name].args)
            else:
                configs[name] = BackendConfig(
                    command=cfg.get("command", name),
                    args=cfg.get("args", []),
                )
        # Detect availability
        for name, bc in configs.items():
            bc.available = bool(shutil.which(bc.command))
        return configs

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop

    # ── tmux helpers (run in executor to avoid blocking) ──

    def _sanitize_for_tmux(self, text: str) -> str:
        """Sanitize text for safe tmux input: strip control chars, truncate."""
        # Strip control characters (\x00-\x1f) except newline (\x0a)
        sanitized = re.sub(r'[\x00-\x09\x0b-\x1f]', '', text)
        # Truncate to 2000 chars
        return sanitized[:2000]

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
            sanitized = self._sanitize_for_tmux(text)
            result = await self._run_tmux(["send-keys", "-t", session, sanitized, "Enter"])
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

    # ── Backend resolution ──

    def _resolve_backend_command(self, backend: str) -> tuple[str, list[str]]:
        """Resolve backend name to (command, args). Falls back to default backend."""
        backend_config = self.config.backends.get(backend)
        if not backend_config:
            log.warning(f"Unknown backend '{backend}', falling back to '{self.config.default_backend}'")
            backend_config = self.config.backends.get(self.config.default_backend, {})
        cmd = backend_config.get("command", "claude")
        args = backend_config.get("args", [])
        if not shutil.which(cmd):
            raise ValueError(f"Backend '{backend}' command not found: {cmd}")
        return cmd, args

    # ── Core operations ──

    async def spawn(
        self,
        role: str = "general",
        name: str | None = None,
        working_dir: str | None = None,
        task: str = "",
        plan_mode: bool = False,
        backend: str = "claude-code",
        model: str | None = None,
        tools: list[str] | None = None,
        system_prompt_extra: str | None = None,
        resume_session: str | None = None,
    ) -> Agent:
        """Spawn a new agent. Returns the Agent object."""
        if len(self.agents) >= self.config.max_agents:
            raise ValueError(f"Maximum agents ({self.config.max_agents}) reached")

        # ── Input validation ──

        # Validate and sanitize name
        if name:
            name = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
            if not name:
                raise ValueError("Agent name cannot be empty after sanitization")

        # Validate task length
        if task and len(task) > 10000:
            raise ValueError("Task description exceeds 10000 character limit")

        # Validate backend exists in config or is demo mode
        if not self.config.demo_mode:
            if backend not in self.config.backends:
                raise ValueError(f"Unknown backend '{backend}'. Available: {', '.join(self.config.backends.keys())}")

        # Validate working_dir
        if working_dir:
            working_dir = os.path.abspath(os.path.expanduser(working_dir))
            home_dir = str(Path.home())
            config_dirs = [str(ASHLAR_DIR)]
            allowed_prefixes = [home_dir] + config_dirs
            if not any(working_dir.startswith(prefix) for prefix in allowed_prefixes):
                if not os.path.isdir(working_dir):
                    raise ValueError(f"Working directory does not exist and is outside home: {working_dir}")

        # Generate ID
        agent_id = uuid.uuid4().hex[:4]
        while agent_id in self.agents:
            agent_id = uuid.uuid4().hex[:4]

        # Generate name if not provided
        if not name:
            role_name = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"]).name.split()[0].lower()
            name = f"{role_name}-{agent_id}"

        # Detect name collisions — append agent ID suffix if collision found
        existing_names = {a.name for a in self.agents.values()}
        if name in existing_names:
            name = f"{name}-{agent_id}"

        # Resolve working directory
        if not working_dir:
            working_dir = self.config.default_working_dir
        working_dir = os.path.expanduser(working_dir)
        working_dir = os.path.abspath(working_dir)
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
            model=model,
            tools_allowed=tools,
            system_prompt=system_prompt_extra,
            _spawn_time=time.monotonic(),
        )
        agent.last_output_time = time.monotonic()
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
            # Real mode: launch backend CLI using BackendConfig capabilities
            bc = self.backend_configs.get(backend)
            if not bc or not bc.available:
                try:
                    cmd_bin, cmd_args = self._resolve_backend_command(backend)
                    bc = BackendConfig(command=cmd_bin, args=cmd_args, available=True)
                except ValueError as e:
                    agent.status = "error"
                    agent.error_message = str(e)
                    log.error(f"Backend resolution failed for {backend}: {e}")
                    return agent

            cmd_parts = [bc.command]

            # Always include configured args, then add auto-approve flag if not already present
            cmd_parts.extend(bc.args)
            if bc.auto_approve_flag and bc.auto_approve_flag not in bc.args:
                cmd_parts.append(bc.auto_approve_flag)

            # Model selection
            if model and bc.supports_model_select:
                cmd_parts.extend(["--model", model])

            # Tool restriction
            if tools and bc.supports_tool_restriction:
                cmd_parts.extend(["--allowedTools", ",".join(tools)])

            # System prompt injection (role context + predecessor output)
            role_obj = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"])
            role_prompt = f"You are a {role_obj.name}. {role_obj.system_prompt}"
            full_system = role_prompt
            if system_prompt_extra:
                full_system += f"\n\n{system_prompt_extra}"
            if bc.supports_system_prompt:
                cmd_parts.extend(["--append-system-prompt", full_system])
                agent.system_prompt = full_system

            # Session resume
            if resume_session and bc.supports_session_resume:
                cmd_parts.extend(["--resume", resume_session])
                agent.session_id = resume_session

            # Build command string
            cmd = " ".join(f"'{p}'" if " " in p else p for p in cmd_parts)
            await self._tmux_send_keys(session_name, cmd)

            # Wait for CLI to start up
            await asyncio.sleep(3)

            # Send task as first message
            if bc.supports_system_prompt:
                # System prompt already injected via CLI flag — just send task
                if task:
                    await self._tmux_send_keys(session_name, task)
            else:
                # Fallback: send role context + task as first message
                if role_obj and role_obj.system_prompt and task:
                    full_message = f"{role_obj.system_prompt}\n\nYour task: {task}"
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

        self._cleanup_file_activity(agent_id)
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

    async def restart(self, agent_id: str) -> bool:
        """Restart an agent by killing its tmux session and re-spawning with same config.
        Updates agent fields in-place on success; sets error on failure without deleting."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        # Prevent concurrent restarts
        if agent._restart_in_progress:
            log.warning(f"Restart already in progress for agent {agent_id}")
            return False

        agent._restart_in_progress = True
        try:
            log.info(f"Restarting agent {agent_id} ({agent.name}), attempt {agent.restart_count + 1}")

            # Save config references (including orchestration fields)
            saved_role = agent.role
            saved_name = agent.name
            saved_working_dir = agent.working_dir
            saved_backend = agent.backend
            saved_task = agent.task
            saved_model = agent.model
            saved_tools = agent.tools_allowed
            saved_system_prompt = agent.system_prompt

            # Kill the old tmux session
            old_tmux_session = agent.tmux_session
            try:
                await self._tmux_send_keys(old_tmux_session, "/exit")
                await asyncio.sleep(1)
            except Exception:
                pass
            try:
                await self._run_tmux(["kill-session", "-t", old_tmux_session])
            except Exception:
                pass

            # Verify old session is gone
            try:
                check = await self._run_tmux(["has-session", "-t", old_tmux_session])
                if check.returncode == 0:
                    await self._run_tmux(["kill-session", "-t", old_tmux_session])
            except Exception:
                pass

            # Clean up demo script temp file
            if agent.script_path:
                try:
                    Path(agent.script_path).unlink(missing_ok=True)
                except Exception:
                    pass

            # Create NEW tmux session (same session name)
            session_name = f"{self.tmux_prefix}-{agent_id}"
            now = datetime.now(timezone.utc).isoformat()

            try:
                result = await self._run_tmux([
                    "new-session", "-d", "-s", session_name,
                    "-x", "200", "-y", "50",
                    "-c", saved_working_dir,
                ])
                if result.returncode != 0:
                    # Failed to create new session — set agent to error, don't delete
                    agent.status = "error"
                    agent.error_message = f"Restart failed: could not create tmux session: {result.stderr}"
                    agent._error_entered_at = time.monotonic()
                    agent.updated_at = now
                    return False
            except Exception as e:
                agent.status = "error"
                agent.error_message = f"Restart failed: {e}"
                agent._error_entered_at = time.monotonic()
                agent.updated_at = now
                return False

            # SUCCESS: new tmux session created. Update agent fields in-place.
            agent.tmux_session = session_name
            agent.status = "spawning"
            agent.summary = "Restarting..."
            agent.restart_count += 1
            agent.last_restart_time = time.monotonic()
            agent.restarted_at = now
            agent.updated_at = now
            agent.error_message = None
            agent.needs_input = False
            agent.input_prompt = None
            agent._spawn_time = time.monotonic()
            agent.last_output_time = time.monotonic()
            agent._prev_output_hash = 0
            agent._first_output_received = False
            agent.output_lines.clear()
            agent._total_chars = 0
            agent.context_pct = 0.0
            agent.script_path = None

            # Get pane PID
            agent.pid = await self._tmux_get_pane_pid(session_name)

            if self.config.demo_mode:
                demo_script = self._build_demo_script(saved_role, saved_task, agent)
                await self._tmux_send_keys(session_name, demo_script)
            else:
                # Use BackendConfig-based command building (same as spawn)
                bc = self.backend_configs.get(saved_backend)
                if not bc or not bc.available:
                    try:
                        cmd_bin, cmd_args = self._resolve_backend_command(saved_backend)
                        bc = BackendConfig(command=cmd_bin, args=cmd_args, available=True)
                    except ValueError as e:
                        agent.status = "error"
                        agent.error_message = str(e)
                        return False

                cmd_parts = [bc.command]
                cmd_parts.extend(bc.args)
                if bc.auto_approve_flag and bc.auto_approve_flag not in bc.args:
                    cmd_parts.append(bc.auto_approve_flag)

                if saved_model and bc.supports_model_select:
                    cmd_parts.extend(["--model", saved_model])
                if saved_tools and bc.supports_tool_restriction:
                    cmd_parts.extend(["--allowedTools", ",".join(saved_tools)])

                # Rebuild system prompt from role + saved extra
                role_obj = BUILTIN_ROLES.get(saved_role, BUILTIN_ROLES["general"])
                if bc.supports_system_prompt and saved_system_prompt:
                    cmd_parts.extend(["--append-system-prompt", saved_system_prompt])

                cmd = " ".join(f"'{p}'" if " " in p else p for p in cmd_parts)
                await self._tmux_send_keys(session_name, cmd)
                await asyncio.sleep(3)

                # Send task
                if bc.supports_system_prompt:
                    if saved_task:
                        await self._tmux_send_keys(session_name, saved_task)
                else:
                    if role_obj and role_obj.system_prompt and saved_task:
                        full_message = f"{role_obj.system_prompt}\n\nYour task: {saved_task}"
                        for line in full_message.split("\n"):
                            if line.strip():
                                await self._tmux_send_keys(session_name, line)
                                await asyncio.sleep(0.1)
                    elif saved_task:
                        await self._tmux_send_keys(session_name, saved_task)

            agent.status = "working"
            agent.updated_at = datetime.now(timezone.utc).isoformat()
            log.info(f"Agent {agent_id} ({saved_name}) restarted successfully (attempt {agent.restart_count})")
            return True
        finally:
            agent._restart_in_progress = False

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

        # Apply secret redaction
        new_lines = [redact_secrets(line) for line in new_lines]

        # Archive overflow lines before they're dropped by the deque
        overflow_count = len(agent.output_lines) + len(new_lines) - agent.output_lines.maxlen
        if overflow_count > 0:
            overflow_lines = list(agent.output_lines)[:overflow_count]
            agent._overflow_to_archive = (agent_id, overflow_lines, agent._archived_lines)
            agent._archived_lines += len(overflow_lines)

        # Update ring buffer (only new lines, not the full capture)
        for line in new_lines:
            agent.output_lines.append(line)

        # Track total chars for context estimation and cost tracking
        chars_added = sum(len(l) for l in new_lines)
        agent._total_chars += chars_added
        # ~3.5 chars/token for English, 200K context window
        # Include base context: task + system prompt + overhead
        base_chars = len(agent.task or '') + len(agent.system_prompt or '') + 1750  # tool/overhead
        total_context_chars = agent._total_chars + base_chars
        agent.context_pct = min(1.0, (total_context_chars / 3.5) / 200000)

        # Token/cost estimation (approximate — marked as estimates in API)
        tokens_est = int(chars_added / 3.5)
        agent.tokens_output += tokens_est
        # Input includes base context: task + system prompt + overhead
        base_input_chars = len(agent.task or '') + len(agent.system_prompt or '') + 2000  # overhead
        agent.tokens_input = int(base_input_chars / 3.5) + int(agent.tokens_output * 0.3)
        # Compute cost from backend config rates
        bc = self.backend_configs.get(agent.backend)
        if bc:
            agent.estimated_cost_usd = (
                (agent.tokens_input / 1000) * bc.cost_input_per_1k +
                (agent.tokens_output / 1000) * bc.cost_output_per_1k
            )

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

        # Get backend-specific patterns if available
        bc = self.backend_configs.get(agent.backend)
        bp = bc.status_patterns if bc else None
        return parse_agent_status(recent, agent, bp)

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

    # ── Workflow dependency resolution ──

    def _get_ready_indices(self, wf_run: WorkflowRun) -> list[int]:
        """Get spec indices whose dependencies are all satisfied."""
        ready = []
        for idx in list(wf_run.pending_indices):
            spec = wf_run.agent_specs[idx]
            deps = spec.get("depends_on", [])
            if not deps:
                ready.append(idx)
                continue
            # Check if all deps have completed
            all_done = all(
                wf_run.agent_map.get(d) in wf_run.completed_ids
                for d in deps
            )
            if all_done:
                ready.append(idx)
        return ready

    def _build_dep_context(self, wf_run: WorkflowRun, spec_idx: int) -> str:
        """Build structured context from predecessor agents."""
        spec = wf_run.agent_specs[spec_idx]
        deps = spec.get("depends_on", [])
        if not deps:
            return ""
        parts = ["## Context from predecessor agents:\n"]
        for dep_idx in deps:
            dep_agent_id = wf_run.agent_map.get(dep_idx)
            if not dep_agent_id:
                continue
            dep_agent = self.agents.get(dep_agent_id)
            if dep_agent:
                dep_spec = wf_run.agent_specs[dep_idx]
                parts.append(f"### Agent '{dep_agent.name}' ({dep_spec.get('role', 'general')})")
                parts.append(f"Task: {dep_agent.task}")
                parts.append(f"Summary: {dep_agent.summary}")
                # Files touched by this agent
                agent_files = [
                    fp for fp, agents in self.file_activity.items()
                    if dep_agent_id in agents
                ]
                if agent_files:
                    parts.append(f"Files touched: {', '.join(agent_files[:20])}")
                # Filter substantive output lines (skip progress bars, short noise, spinners)
                _noise_re = re.compile(r'^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏\s█░▓▒]*$|^\s{0,2}$')
                recent = list(dep_agent.output_lines)[-30:]
                substantive = [
                    _strip_ansi(l) for l in recent
                    if len(l.strip()) >= 5 and not _noise_re.match(l)
                ][-15:]
                if substantive:
                    parts.append(f"Recent output:\n```\n{chr(10).join(substantive)}\n```")
                parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _resolve_skip_val(token: str, ctx: dict[str, str]) -> str | None:
        """Resolve a token in a skip_if expression to a string value."""
        token = token.strip()
        # String literal: 'value' or "value"
        if (token.startswith("'") and token.endswith("'")) or (token.startswith('"') and token.endswith('"')):
            return token[1:-1]
        # Context variable
        if token in ctx:
            return ctx[token]
        return None

    def _evaluate_skip_if(self, wf_run: WorkflowRun, spec_idx: int) -> bool:
        """Evaluate skip_if condition. Returns True if the agent should be skipped.

        Supports safe expressions like:
          "prev.status == 'complete'"
          "prev.status != 'error'"
          "'keyword' in prev.summary"
          "'keyword' not in prev.summary"
        """
        spec = wf_run.agent_specs[spec_idx]
        skip_if = spec.get("skip_if")
        if not skip_if:
            return False
        deps = spec.get("depends_on", [])
        if not deps:
            log.warning("skip_if set on spec %d but no depends_on — condition cannot reference prev, skipping evaluation", spec_idx)
            return False
        for dep_idx in deps:
            dep_agent_id = wf_run.agent_map.get(dep_idx)
            if dep_agent_id:
                dep_agent = self.agents.get(dep_agent_id)
                if dep_agent:
                    ctx = {"prev.status": dep_agent.status, "prev.summary": dep_agent.summary or ""}
                    try:
                        return self._safe_eval_condition(skip_if, ctx)
                    except Exception:
                        return False
        return False

    @staticmethod
    def _safe_eval_condition(expr: str, ctx: dict[str, str]) -> bool:
        """Evaluate a simple comparison expression without eval().

        Supports: ==, !=, in, not in operators on string values.
        """
        expr = expr.strip()

        # Try "not in" first (before "in" to avoid partial match)
        for op, negate in [("not in", True), ("in", False), ("!=", False), ("==", False)]:
            if op in expr:
                parts = expr.split(op, 1)
                if len(parts) != 2:
                    return False
                left = parts[0].strip()
                right = parts[1].strip()

                left_val = AgentManager._resolve_skip_val(left, ctx)
                right_val = AgentManager._resolve_skip_val(right, ctx)

                if left_val is None or right_val is None:
                    return False

                if op == "==":
                    return left_val == right_val
                elif op == "!=":
                    return left_val != right_val
                elif op == "in":
                    return left_val in right_val
                elif op == "not in":
                    return left_val not in right_val
        return False

    async def resolve_workflow_deps(self, wf_run: WorkflowRun, hub: Any = None) -> None:
        """Check and spawn any agents whose deps are now satisfied."""
        if wf_run.status != "running":
            return
        ready = self._get_ready_indices(wf_run)
        for idx in ready:
            spec = wf_run.agent_specs[idx]
            wf_run.pending_indices.discard(idx)

            # Check skip_if condition
            if self._evaluate_skip_if(wf_run, idx):
                wf_run.completed_ids.add(f"skipped_spec_{idx}")
                if hub:
                    await hub.broadcast_event(
                        "workflow_agent_skipped",
                        f"Pipeline '{wf_run.workflow_name}': agent '{spec.get('name', 'unnamed')}' skipped (condition met)",
                        metadata={"workflow_run_id": wf_run.id, "spec_index": idx},
                    )
                continue

            # Build context from predecessors
            dep_context = self._build_dep_context(wf_run, idx)
            try:
                agent = await self.spawn(
                    role=spec.get("role", "general"),
                    name=spec.get("name"),
                    working_dir=spec.get("working_dir") or wf_run.working_dir,
                    task=spec.get("task", ""),
                    backend=spec.get("backend", self.config.default_backend),
                    model=spec.get("model"),
                    tools=spec.get("tools"),
                    system_prompt_extra=dep_context if dep_context else None,
                )
                wf_run.agent_map[idx] = agent.id
                wf_run.running_ids.add(agent.id)
                agent.workflow_run_id = wf_run.id
                agent.related_agents = [
                    aid for aid in list(wf_run.running_ids) + list(wf_run.completed_ids)
                    if aid != agent.id
                ]
                if hub:
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast_event(
                        "workflow_agent_spawned",
                        f"Pipeline '{wf_run.workflow_name}': agent {agent.name} started (deps satisfied)",
                        agent.id, agent.name,
                        {"workflow_run_id": wf_run.id, "spec_index": idx},
                    )
            except ValueError as e:
                log.warning(f"Workflow dep spawn failed for spec {idx}: {e}")
                wf_run.failed_ids.add(f"spec_{idx}")

        # Check if workflow is complete or failed
        if not wf_run.pending_indices and not wf_run.running_ids:
            wf_run.status = "completed" if not wf_run.failed_ids else "failed"
            wf_run.completed_at = datetime.now(timezone.utc).isoformat()
            if hub:
                await hub.broadcast_event(
                    f"workflow_{wf_run.status}",
                    f"Workflow '{wf_run.workflow_name}' {wf_run.status}",
                    metadata={"workflow_run_id": wf_run.id},
                )

    def on_agent_complete(self, agent_id: str) -> WorkflowRun | None:
        """Called when an agent completes. Returns the WorkflowRun if the agent was part of one."""
        for wf_run in self.workflow_runs.values():
            if agent_id in wf_run.running_ids:
                wf_run.running_ids.discard(agent_id)
                wf_run.completed_ids.add(agent_id)
                return wf_run
        return None

    def on_agent_failed(self, agent_id: str) -> tuple[WorkflowRun | None, str]:
        """Called when an agent fails. Returns (WorkflowRun, action) where action is 'abort'|'skip'|'retry'."""
        for wf_run in self.workflow_runs.values():
            if agent_id in wf_run.running_ids:
                wf_run.running_ids.discard(agent_id)

                # Find which spec this agent belongs to
                failed_idx = None
                for idx, aid in wf_run.agent_map.items():
                    if aid == agent_id:
                        failed_idx = idx
                        break

                if failed_idx is not None:
                    spec = wf_run.agent_specs[failed_idx]
                    on_failure = spec.get("on_failure", "abort")
                    retry_count = spec.get("retry_count", 0)
                    current_retries = spec.get("_retries", 0)

                    if on_failure == "retry" and current_retries < min(retry_count, 3):
                        # Re-add to pending for retry
                        spec["_retries"] = current_retries + 1
                        wf_run.pending_indices.add(failed_idx)
                        del wf_run.agent_map[failed_idx]
                        return wf_run, "retry"

                    if on_failure == "skip":
                        # Treat as completed so downstream can proceed
                        wf_run.completed_ids.add(agent_id)
                        return wf_run, "skip"

                    # Default: abort — block downstream
                    wf_run.failed_ids.add(agent_id)
                    to_block = set()
                    for idx in list(wf_run.pending_indices):
                        deps = wf_run.agent_specs[idx].get("depends_on", [])
                        if failed_idx in deps:
                            to_block.add(idx)
                    for idx in to_block:
                        wf_run.pending_indices.discard(idx)
                        wf_run.failed_ids.add(f"blocked_spec_{idx}")
                    return wf_run, "abort"
                else:
                    wf_run.failed_ids.add(agent_id)
                    return wf_run, "abort"
        return None, "abort"

    # ── File conflict detection ──

    # Separate read vs write patterns for smarter conflict detection
    _FILE_WRITE_RE = re.compile(
        r"(?i)(?:writing|editing|creating|modifying|updating|Tool Use:.*(?:Edit|Write|Create))\s+[\'\"]?([/\w\-./]+\.\w{1,8})[\'\"]?"
    )
    _FILE_READ_RE = re.compile(
        r"(?i)(?:reading|loading|scanning|Tool Use:.*(?:Read|Glob))\s+[\'\"]?([/\w\-./]+\.\w{1,8})[\'\"]?"
    )

    def _check_file_conflicts(self, agent_id: str, lines: list[str]) -> list[dict]:
        """Parse output for file operations and check for write-write conflicts.
        Read-write overlaps generate softer warnings. Read-read is ignored."""
        conflicts = []
        agent = self.agents.get(agent_id)
        if not agent:
            return conflicts

        for line in lines:
            stripped = _strip_ansi(line)

            # Check writes first (higher severity)
            write_match = self._FILE_WRITE_RE.search(stripped)
            if write_match:
                file_path = write_match.group(1)
                if file_path not in self.file_activity:
                    self.file_activity[file_path] = {}
                self.file_activity[file_path][agent_id] = "write"
                # Track files touched count
                agent.files_touched = len([
                    fp for fp, agents in self.file_activity.items()
                    if agent_id in agents
                ])

                # Check for write-write conflicts with other active agents
                for other_id, op in self.file_activity[file_path].items():
                    if other_id == agent_id:
                        continue
                    other = self.agents.get(other_id)
                    if other and other.status in ("working", "planning"):
                        severity = "conflict" if op == "write" else "warning"
                        conflicts.append({
                            "file_path": file_path,
                            "agent_id": agent_id,
                            "agent_name": agent.name,
                            "other_agent_id": other_id,
                            "other_agent_name": other.name,
                            "severity": severity,
                        })
                continue

            # Check reads (lower severity, track but don't conflict with other reads)
            read_match = self._FILE_READ_RE.search(stripped)
            if read_match:
                file_path = read_match.group(1)
                if file_path not in self.file_activity:
                    self.file_activity[file_path] = {}
                if agent_id not in self.file_activity[file_path]:
                    self.file_activity[file_path][agent_id] = "read"
                # Track files touched count
                agent.files_touched = len([
                    fp for fp, agents in self.file_activity.items()
                    if agent_id in agents
                ])

        return conflicts

    def _cleanup_file_activity(self, agent_id: str) -> None:
        """Remove an agent from all file activity tracking."""
        for file_path in list(self.file_activity.keys()):
            self.file_activity[file_path].pop(agent_id, None)
            if not self.file_activity[file_path]:
                del self.file_activity[file_path]

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
    "reading": [
        re.compile(r"(?i)(reading|loading|scanning|parsing) .+\.\w+"),
        re.compile(r"(?i)(reading|loading|scanning|parsing) (directory|folder|project|codebase)"),
        re.compile(r"(?i)exploring .+"),
    ],
    "working": [
        re.compile(r"(?i)(writing|creating|editing|updating) \S+\.\w+"),
        re.compile(r"(?i)(running|executing) .+"),
        re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"),
        re.compile(r"█+░*"),
        re.compile(r"(?i)Tool Use:"),
        re.compile(r"(?i)Bash:"),
        re.compile(r"(?i)files? (created|edited|read)"),
        re.compile(r"(?i)(checking|auditing|analyzing|testing)"),
        # Git operations
        re.compile(r"(?i)(git (add|commit|push|pull|checkout|merge|rebase))"),
        # Build/compile operations
        re.compile(r"(?i)(building|compiling|bundling|webpack|vite|esbuild)"),
        # Test result patterns (working, not complete — tests are still running)
        re.compile(r"(?i)(\d+ (tests?|specs?) (passed|failed|skipped))"),
    ],
    "waiting": [
        re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
        re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
        re.compile(r"(?i)proceed\?"),
        re.compile(r"(?i)\bapprove\b"),
    ],
    "error": [
        # Fatal patterns — actual crashes, not just mentions of "error" in output
        re.compile(r"(?i)\b(traceback|fatal|panic|segfault|SIGKILL|SIGSEGV)\b"),
        re.compile(r"(?i)unhandled (exception|error|rejection)"),
        re.compile(r"(?i)command not found"),
        re.compile(r"(?i)permission denied"),
        re.compile(r"(?i)(cannot|couldn'?t) (connect|reach|find|open|read|write)"),
        re.compile(r"(?i)out of memory"),
        re.compile(r"(?i)killed by signal"),
    ],
    # Non-fatal error mentions — tracked for health scoring but don't flip status
    "error_mention": [
        re.compile(r"(?i)(?<!\bno\s)\b(error|exception|failed)\b(?!\s*handl)"),
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


def parse_agent_status(recent_lines: list[str], agent: Agent, backend_patterns: dict[str, list[str]] | None = None) -> str:
    """Parse recent terminal output to detect agent status.
    Priority: waiting > error > reading > planning > working > complete > current status.
    Tracks non-fatal error mentions for health scoring without flipping status.
    backend_patterns override defaults for specific status categories."""
    text_block = "\n".join(recent_lines)
    tail_text = "\n".join(recent_lines[-5:])

    # Merge backend-specific patterns with defaults
    effective_patterns: dict[str, list] = {}
    for cat, pats in STATUS_PATTERNS.items():
        effective_patterns[cat] = list(pats)
    if backend_patterns:
        for cat, str_pats in backend_patterns.items():
            if cat not in effective_patterns:
                effective_patterns[cat] = []
            for sp in str_pats:
                try:
                    effective_patterns[cat].append(re.compile(sp))
                except re.error:
                    pass

    # Track non-fatal error mentions for health scoring (don't affect status)
    for pattern in effective_patterns.get("error_mention", []):
        if pattern.search(tail_text):
            agent.error_count = min(agent.error_count + 1, 100)

    # Check for waiting (highest priority)
    for pattern in effective_patterns["waiting"]:
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

    # Check for fatal error (only in last 5 lines to avoid old mentions)
    for pattern in effective_patterns["error"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "error"

    # Check for reading (before working, more specific)
    for pattern in effective_patterns["reading"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "reading"

    # Check for planning
    for pattern in effective_patterns["planning"]:
        if pattern.search(text_block):
            agent.needs_input = False
            return "planning"

    # Check for complete
    for pattern in effective_patterns["complete"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "idle"

    # Check for working
    for pattern in effective_patterns["working"]:
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
            return _strip_ansi(f"Tests: {m.group(1)} passed, {m.group(2)} failed")
        m = _COVERAGE_RE.search(line)
        if m:
            return _strip_ansi(f"Coverage: {m.group(1)}%")

    # Extract file paths being worked on
    for line in reversed(lines[-20:]):
        stripped = _strip_ansi(line.strip())
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
        m = _FILES_PROGRESS_RE.search(_strip_ansi(line))
        if m:
            return f"Progress: {m.group(1)} of {m.group(2)} files"

    # Fallback: last non-empty line
    for line in reversed(lines[-10:]):
        stripped = _strip_ansi(line.strip())
        if stripped and len(stripped) > 5:
            return stripped[:100]

    return _strip_ansi(task[:100]) if task else "Working..."


# ── LLM-Powered Summary Generation ──

class LLMSummarizer:
    """Async LLM client for generating rich agent summaries via xAI/OpenAI-compatible API."""

    def __init__(self, config: Config):
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._failures: int = 0
        self._max_failures: int = 5  # Circuit breaker threshold
        self._circuit_reset_time: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def summarize(self, output_lines: list[str], task: str, role: str, status: str) -> str | None:
        """Generate a summary from agent output. Returns None on failure (use heuristic fallback)."""
        if not self.config.llm_enabled or not self.config.llm_api_key:
            return None

        # Circuit breaker: if too many failures, back off
        if self._failures >= self._max_failures:
            if time.monotonic() < self._circuit_reset_time:
                return None
            # Reset circuit after cooldown
            self._failures = 0

        # Truncate output to configured max lines
        recent = output_lines[-self.config.llm_max_output_lines:]
        if not recent:
            return None

        output_text = _strip_ansi("\n".join(recent))

        prompt = (
            f"You are summarizing an AI coding agent's terminal output.\n"
            f"Agent role: {role}\nAgent status: {status}\nTask: {task}\n\n"
            f"Recent terminal output:\n```\n{output_text}\n```\n\n"
            f"Write a concise 1-sentence summary (max 100 chars) of what the agent is currently doing. "
            f"Focus on the specific action and file/component being worked on. "
            f"Examples: 'Writing auth middleware in src/auth.ts', 'Running test suite — 12/15 passing', "
            f"'Found 2 XSS vulnerabilities in form handler'. Do NOT include quotes."
        )

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.config.llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.3,
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if content:
                        self._failures = 0
                        return content[:100]
                elif resp.status in (401, 403):
                    log.error(f"LLM disabled: authentication failed (HTTP {resp.status}). Check API key.")
                    self.config.llm_enabled = False
                    return None
                elif resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    cooldown = float(retry_after) if retry_after and retry_after.isdigit() else 60.0
                    log.warning(f"LLM rate limited, cooling down for {cooldown}s")
                    self._circuit_reset_time = time.monotonic() + cooldown
                    return None
                else:
                    log.debug(f"LLM API returned {resp.status}")
                    self._failures += 1
        except asyncio.TimeoutError:
            log.debug("LLM summary request timed out")
            self._failures += 1
        except Exception as e:
            log.debug(f"LLM summary error: {e}")
            self._failures += 1

        # Set circuit breaker cooldown
        if self._failures >= self._max_failures:
            self._circuit_reset_time = time.monotonic() + 60.0
            log.warning("LLM circuit breaker tripped, cooling down for 60s")

        return None


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
    agent.phase = phase
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


def calculate_health_score(agent: Agent, memory_limit_mb: int = 2048) -> float:
    """Calculate composite health score (0.0–1.0) from uptime, errors, output rate, memory.

    Components:
      - uptime_factor: ramps up to 1.0 over 10 minutes (longer uptime = healthier)
      - error_factor: fewer errors = better (1.0 at 0 errors, decays toward 0.2)
      - output_factor: some output = good, no output for >60s = concerning
      - memory_factor: under limit = 1.0, degrades linearly above 75% of limit
    """
    now = time.monotonic()

    # Uptime factor: ramp from 0.5 to 1.0 over 600s (10 min)
    if agent._spawn_time > 0:
        uptime_s = now - agent._spawn_time
        uptime_factor = min(1.0, 0.5 + (uptime_s / 1200))  # 0.5 base, full at 10min
    else:
        uptime_factor = 0.5

    # Error factor: exponential decay based on error count
    # 0 errors → 1.0, 5 errors → ~0.6, 10+ errors → ~0.35
    error_factor = max(0.2, 1.0 / (1.0 + agent.error_count * 0.15))

    # Output factor: penalize stale agents (no output for >60s)
    if agent.last_output_time > 0:
        silence_s = now - agent.last_output_time
        if silence_s < 30:
            output_factor = 1.0
        elif silence_s < 120:
            output_factor = max(0.3, 1.0 - (silence_s - 30) / 180)
        else:
            output_factor = 0.3
    elif agent._spawn_time > 0 and (now - agent._spawn_time) > 30:
        # Never received output but agent has been alive >30s
        output_factor = 0.4
    else:
        output_factor = 0.8  # just spawned, no output yet is fine

    # Memory factor: 1.0 under 75% of limit, linear decay above
    if memory_limit_mb > 0 and agent.memory_mb > 0:
        mem_ratio = agent.memory_mb / memory_limit_mb
        if mem_ratio < 0.75:
            memory_factor = 1.0
        else:
            memory_factor = max(0.1, 1.0 - (mem_ratio - 0.75) * 4.0)
    else:
        memory_factor = 1.0

    # Error/paused status override
    if agent.status == "error":
        return max(0.05, error_factor * 0.3)
    if agent.status == "paused":
        return 0.5  # neutral — paused by user, not unhealthy

    # Weighted composite
    score = (
        uptime_factor * 0.20 +
        error_factor * 0.35 +
        output_factor * 0.25 +
        memory_factor * 0.20
    )
    return round(min(1.0, max(0.0, score)), 3)


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

        # SQLite performance PRAGMAs
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA cache_size=-8000")

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
            CREATE TABLE IF NOT EXISTS agent_messages (
                id TEXT PRIMARY KEY,
                from_agent_id TEXT NOT NULL,
                to_agent_id TEXT,
                content TEXT NOT NULL,
                created_at TEXT,
                read_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_agent_id);
            CREATE INDEX IF NOT EXISTS idx_history_completed ON agents_history(completed_at);
            CREATE INDEX IF NOT EXISTS idx_messages_from ON agent_messages(from_agent_id);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON agent_messages(created_at);

            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                agent_id TEXT,
                agent_name TEXT,
                message TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_activity_agent ON activity_events(agent_id);
            CREATE INDEX IF NOT EXISTS idx_activity_type ON activity_events(event_type);

            CREATE TABLE IF NOT EXISTS file_locks (
                file_path TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT,
                locked_at TEXT NOT NULL,
                PRIMARY KEY (file_path, agent_id)
            );

            CREATE TABLE IF NOT EXISTS agent_output_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_output_archive_agent ON agent_output_archive(agent_id, line_offset);

            CREATE TABLE IF NOT EXISTS agent_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'general',
                backend TEXT DEFAULT 'claude-code',
                task TEXT DEFAULT '',
                system_prompt TEXT DEFAULT '',
                model TEXT DEFAULT '',
                tools_allowed TEXT DEFAULT '',
                working_dir TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scratchpad (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT DEFAULT '',
                set_by TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, key)
            );
        """)
        await self._db.commit()

        # Safe migrations: add columns if missing
        try:
            await self._db.execute("ALTER TABLE agent_messages ADD COLUMN message_type TEXT DEFAULT 'text'")
            await self._db.commit()
        except Exception:
            pass  # Column already exists

        try:
            await self._db.execute("ALTER TABLE agents_history ADD COLUMN tokens_input INTEGER DEFAULT 0")
            await self._db.execute("ALTER TABLE agents_history ADD COLUMN tokens_output INTEGER DEFAULT 0")
            await self._db.execute("ALTER TABLE agents_history ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0")
            await self._db.commit()
        except Exception:
            pass  # Columns already exist

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

    # ── Output Archive ──

    async def archive_output(self, agent_id: str, lines: list[str], start_offset: int) -> None:
        """Archive overflow output lines to SQLite."""
        if not self._db or not lines:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.executemany(
                "INSERT INTO agent_output_archive (agent_id, line_offset, content, created_at) VALUES (?, ?, ?, ?)",
                [(agent_id, start_offset + i, line, now) for i, line in enumerate(lines)],
            )
            await self._db.commit()
        except Exception as e:
            log.debug(f"Failed to archive output for {agent_id}: {e}")

    async def get_archived_output(self, agent_id: str, offset: int = 0, limit: int = 500) -> tuple[list[str], int]:
        """Retrieve archived output lines. Returns (lines, total_count)."""
        if not self._db:
            return [], 0
        try:
            async with self._db.execute(
                "SELECT COUNT(*) FROM agent_output_archive WHERE agent_id = ?", (agent_id,)
            ) as cur:
                row = await cur.fetchone()
                total = row[0] if row else 0
            async with self._db.execute(
                "SELECT content FROM agent_output_archive WHERE agent_id = ? ORDER BY line_offset LIMIT ? OFFSET ?",
                (agent_id, limit, offset),
            ) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows], total
        except Exception as e:
            log.debug(f"Failed to get archived output for {agent_id}: {e}")
            return [], 0

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
                backend, created_at, completed_at, duration_sec, context_pct, output_preview,
                tokens_input, tokens_output, estimated_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent.id, agent.name, agent.role, agent.project_id, agent.task,
             agent.summary, agent.status, agent.working_dir, agent.backend,
             agent.created_at, completed_at, duration, agent.context_pct, output_preview,
             agent.tokens_input, agent.tokens_output, agent.estimated_cost_usd),
        )
        await self._db.commit()

    async def get_agent_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM agents_history ORDER BY completed_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_agent_history_count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM agents_history") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

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

    # ── Agent Messages ──

    async def save_message(self, msg: dict) -> None:
        await self._db.execute(
            """INSERT INTO agent_messages (id, from_agent_id, to_agent_id, content, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (msg["id"], msg["from_agent_id"], msg.get("to_agent_id"),
             msg["content"], msg["created_at"]),
        )
        await self._db.commit()

    async def get_messages_for_agent(self, agent_id: str, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM agent_messages WHERE to_agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_messages_between(self, agent_a: str, agent_b: str, limit: int = 50) -> list[dict]:
        async with self._db.execute(
            """SELECT * FROM agent_messages
               WHERE (from_agent_id = ? AND to_agent_id = ?)
                  OR (from_agent_id = ? AND to_agent_id = ?)
               ORDER BY created_at DESC LIMIT ?""",
            (agent_a, agent_b, agent_b, agent_a, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_message_count_for_agent(self, agent_id: str) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE to_agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def mark_messages_read(self, agent_id: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            "UPDATE agent_messages SET read_at = ? WHERE to_agent_id = ? AND read_at IS NULL",
            (now, agent_id),
        ) as cur:
            await self._db.commit()
            return cur.rowcount

    async def get_unread_count(self, agent_id: str) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE to_agent_id = ? AND read_at IS NULL",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # ── Activity Events ──

    async def log_event(
        self,
        event_type: str,
        message: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata) if metadata else None
        try:
            await self._db.execute(
                """INSERT INTO activity_events (event_type, agent_id, agent_name, message, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_type, agent_id, agent_name, message, metadata_json, now),
            )
            await self._db.commit()
        except Exception as e:
            log.debug(f"Failed to log event: {e}")

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        agent_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        conditions = []
        params: list = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        async with self._db.execute(
            f"SELECT * FROM activity_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("metadata_json"):
                    try:
                        d["metadata"] = json.loads(d.pop("metadata_json"))
                    except Exception:
                        d["metadata"] = None
                        d.pop("metadata_json", None)
                else:
                    d.pop("metadata_json", None)
                    d["metadata"] = None
                result.append(d)
            return result

    async def get_events_count(
        self,
        agent_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
    ) -> int:
        conditions = []
        params: list = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._db.execute(
            f"SELECT COUNT(*) FROM activity_events {where}", params
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # ── File Locks ──

    async def set_file_lock(self, file_path: str, agent_id: str, agent_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT OR REPLACE INTO file_locks (file_path, agent_id, agent_name, locked_at) VALUES (?, ?, ?, ?)",
            (file_path, agent_id, agent_name, now),
        )
        await self._db.commit()

    async def get_file_locks(self, file_path: str | None = None) -> list[dict]:
        if file_path:
            async with self._db.execute(
                "SELECT * FROM file_locks WHERE file_path = ?", (file_path,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        else:
            async with self._db.execute("SELECT * FROM file_locks") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def release_file_locks(self, agent_id: str) -> None:
        await self._db.execute("DELETE FROM file_locks WHERE agent_id = ?", (agent_id,))
        await self._db.commit()

    # ── Agent Presets ──

    async def get_presets(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM agent_presets ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_preset(self, preset_id: str) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM agent_presets WHERE id = ?", (preset_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_preset(self, preset: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_presets
               (id, name, role, backend, task, system_prompt, model, tools_allowed, working_dir, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (preset["id"], preset["name"], preset.get("role", "general"),
             preset.get("backend", "claude-code"), preset.get("task", ""),
             preset.get("system_prompt", ""), preset.get("model", ""),
             preset.get("tools_allowed", ""), preset.get("working_dir", ""),
             preset.get("created_at", now), now),
        )
        await self._db.commit()

    async def delete_preset(self, preset_id: str) -> bool:
        async with self._db.execute(
            "DELETE FROM agent_presets WHERE id = ?", (preset_id,)
        ) as cur:
            await self._db.commit()
            return cur.rowcount > 0

    # ── Scratchpad ──

    async def get_scratchpad(self, project_id: str) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM scratchpad WHERE project_id = ? ORDER BY key", (project_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_scratchpad(self, project_id: str, key: str, value: str, set_by: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO scratchpad (project_id, key, value, set_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, key) DO UPDATE SET value=excluded.value, set_by=excluded.set_by, updated_at=excluded.updated_at""",
            (project_id, key, value, set_by, now, now),
        )
        await self._db.commit()

    async def delete_scratchpad(self, project_id: str, key: str) -> bool:
        async with self._db.execute(
            "DELETE FROM scratchpad WHERE project_id = ? AND key = ?", (project_id, key)
        ) as cur:
            await self._db.commit()
            return cur.rowcount > 0


# ─────────────────────────────────────────────
# Section 5c: Rate Limiter
# ─────────────────────────────────────────────


class RateLimiter:
    """Token bucket rate limiter, keyed by client IP."""

    def __init__(self) -> None:
        self._buckets: dict[str, dict] = {}  # ip -> {tokens, last_refill}

    def check(self, ip: str, cost: float = 1.0, rate: float = 1.0, burst: float = 10.0) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds). Rate is tokens/sec, burst is max tokens."""
        now = time.monotonic()
        bucket = self._buckets.get(ip)
        if not bucket:
            bucket = {"tokens": burst, "last_refill": now}
            self._buckets[ip] = bucket

        # Refill tokens
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(burst, bucket["tokens"] + elapsed * rate)
        bucket["last_refill"] = now

        if bucket["tokens"] >= cost:
            bucket["tokens"] -= cost
            return True, 0.0
        else:
            retry_after = (cost - bucket["tokens"]) / rate
            return False, retry_after

    def cleanup_stale(self, max_age: float = 300.0) -> None:
        """Remove buckets not accessed in max_age seconds."""
        now = time.monotonic()
        stale = [ip for ip, b in self._buckets.items() if now - b["last_refill"] > max_age]
        for ip in stale:
            del self._buckets[ip]


def _get_client_ip(request: web.Request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    peername = request.transport.get_extra_info("peername") if request.transport else None
    return peername[0] if peername else "unknown"


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
            backends_info = {}
            for name, bc in self.agent_manager.backend_configs.items():
                d = bc.to_dict()
                d["name"] = name
                backends_info[name] = d
            try:
                presets = await self.db.get_presets()
            except Exception:
                presets = []
            await ws.send_json({
                "type": "sync",
                "agents": [a.to_dict() for a in self.agent_manager.agents.values()],
                "projects": projects,
                "workflows": workflows,
                "presets": presets,
                "config": self.config.to_dict(),
                "backends": backends_info,
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
                    await self.broadcast_event("agent_spawned", f"Agent {agent.name} spawned", agent.id, agent.name)
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
                        await self.broadcast_event("agent_killed", f"Agent {name} killed", agent_id, name)

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

            case "agent_message":
                from_id = data.get("from_agent_id")
                to_id = data.get("to_agent_id")
                content = data.get("content", "")
                if from_id and to_id and content and self.db:
                    to_agent = self.agent_manager.agents.get(to_id)
                    if not to_agent:
                        await ws.send_json({"type": "error", "message": f"Target agent {to_id} not found"})
                    else:
                        msg = {
                            "id": uuid.uuid4().hex[:8],
                            "from_agent_id": from_id,
                            "to_agent_id": to_id,
                            "content": content,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self.db.save_message(msg)
                        to_agent.unread_messages += 1
                        # Also send to agent's tmux session
                        from_agent = self.agent_manager.agents.get(from_id)
                        from_name = from_agent.name if from_agent else from_id
                        sanitized = content.strip()[:500]
                        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)  # Strip control chars except newline/tab
                        await self.agent_manager.send_message(to_id, f"[Message from {from_name}]: {sanitized}")
                        await self.broadcast({"type": "agent_message", "message": msg})
                        await self.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})

            case "sync_request":
                projects = await self.db.get_projects() if self.db else []
                workflows = await self.db.get_workflows() if self.db else []
                try:
                    presets = await self.db.get_presets() if self.db else []
                except Exception:
                    presets = []
                backends_info = {}
                for name, bc in self.agent_manager.backend_configs.items():
                    d = bc.to_dict()
                    d["name"] = name
                    backends_info[name] = d
                await ws.send_json({
                    "type": "sync",
                    "agents": [a.to_dict() for a in self.agent_manager.agents.values()],
                    "projects": projects,
                    "workflows": workflows,
                    "presets": presets,
                    "config": self.config.to_dict(),
                    "backends": backends_info,
                })

            case _:
                await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        dead: set[web.WebSocketResponse] = set()

        async def _send(ws: web.WebSocketResponse) -> None:
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=5.0)
            except (ConnectionError, RuntimeError, ConnectionResetError, asyncio.TimeoutError):
                dead.add(ws)

        await asyncio.gather(*[_send(ws) for ws in self.clients], return_exceptions=True)
        self.clients -= dead

    async def broadcast_event(
        self,
        event: str,
        message: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Broadcast an event to WebSocket clients AND persist to activity_events table."""
        payload: dict[str, Any] = {"type": "event", "event": event, "message": message}
        if agent_id:
            payload["agent_id"] = agent_id
        if metadata:
            payload["metadata"] = metadata
        await self.broadcast(payload)
        if self.db:
            await self.db.log_event(event, message, agent_id, agent_name, metadata)


# ─────────────────────────────────────────────
# Section 8: REST API Handlers
# ─────────────────────────────────────────────

# ── Auth Middleware ──

@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    """Token-based auth middleware. Skips for /, /logo.png, /api/health, /api/auth/verify."""
    config: Config = request.app["config"]
    if not config.require_auth:
        return await handler(request)

    # Skip auth for public routes
    path = request.path
    if path in ("/", "/logo.png", "/api/health", "/api/auth/verify", "/ws"):
        # For WebSocket, check token in query params
        if path == "/ws":
            token = request.query.get("token", "")
            if token != config.auth_token:
                return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    # Check Authorization header or query param
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.query.get("token", "")

    if token != config.auth_token:
        return web.json_response({"error": "Unauthorized"}, status=401)

    return await handler(request)


async def verify_auth(request: web.Request) -> web.Response:
    """POST /api/auth/verify — validate an auth token."""
    config: Config = request.app["config"]
    if not config.require_auth:
        return web.json_response({"valid": True, "auth_required": False})

    try:
        data = await request.json()
    except Exception:
        data = {}
    token = data.get("token", "")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    valid = token == config.auth_token
    return web.json_response({"valid": valid, "auth_required": True})


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


def _check_rate(request: web.Request, cost: float = 1.0, rate: float = 5.0, burst: float = 10.0) -> web.Response | None:
    """Check rate limit. Returns a 429 response if exceeded, None if OK."""
    rl: RateLimiter = request.app["rate_limiter"]
    ip = _get_client_ip(request)
    allowed, retry_after = rl.check(ip, cost, rate, burst)
    if not allowed:
        return web.json_response(
            {"error": "Too many requests", "retry_after": round(retry_after, 1)},
            status=429,
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    return None


async def spawn_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    if r := _check_rate(request, cost=3, rate=1.0, burst=5):
        return r
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # REST-level input validation (before calling manager.spawn)
    name = data.get("name")
    if name is not None:
        if not isinstance(name, str):
            return web.json_response({"error": "name must be a string"}, status=400)
        name = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
        if not name:
            return web.json_response({"error": "name cannot be empty"}, status=400)

    task = data.get("task", "")
    if not isinstance(task, str):
        return web.json_response({"error": "task must be a string"}, status=400)
    if len(task) > 10000:
        return web.json_response({"error": "task exceeds 10000 character limit"}, status=400)

    role = data.get("role", request.app["config"].default_role)
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)

    backend = data.get("backend", "claude-code")
    if not isinstance(backend, str):
        return web.json_response({"error": "backend must be a string"}, status=400)

    working_dir = data.get("working_dir")
    if working_dir is not None and not isinstance(working_dir, str):
        return web.json_response({"error": "working_dir must be a string"}, status=400)

    # Optional orchestration fields
    model_sel = data.get("model")
    if model_sel is not None and not isinstance(model_sel, str):
        return web.json_response({"error": "model must be a string"}, status=400)
    tools_sel = data.get("tools")
    if tools_sel is not None and not isinstance(tools_sel, list):
        return web.json_response({"error": "tools must be a list of strings"}, status=400)
    system_prompt_extra = data.get("system_prompt_extra")
    if system_prompt_extra is not None and not isinstance(system_prompt_extra, str):
        return web.json_response({"error": "system_prompt_extra must be a string"}, status=400)
    resume_session = data.get("resume_session")

    try:
        agent = await manager.spawn(
            role=role,
            name=name,
            working_dir=working_dir,
            task=task,
            plan_mode=data.get("plan_mode", False),
            backend=backend,
            model=model_sel,
            tools=tools_sel,
            system_prompt_extra=system_prompt_extra,
            resume_session=resume_session,
        )

        # Broadcast to WebSocket clients
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        await hub.broadcast_event("agent_spawned", f"Agent {agent.name} spawned", agent.id, agent.name)

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
        await hub.broadcast({"type": "agent_removed", "agent_id": agent_id})
        await hub.broadcast_event("agent_killed", f"Agent {name} killed", agent_id, name)
        return web.json_response({"status": "killed"})
    return web.json_response({"error": "Failed to kill agent"}, status=500)


async def send_to_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    if r := _check_rate(request, cost=1, rate=5.0, burst=15):
        return r
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


async def restart_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/restart — Manually restart an agent."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    if r := _check_rate(request, cost=2, rate=0.5, burst=3):
        return r
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        success = await manager.restart(agent_id)
        if success:
            restarted = manager.agents.get(agent_id)
            if restarted:
                await hub.broadcast({"type": "agent_update", "agent": restarted.to_dict()})
                await hub.broadcast_event(
                    "agent_restarted",
                    f"Agent {restarted.name} manually restarted (attempt {restarted.restart_count})",
                    agent_id, restarted.name,
                )
                return web.json_response({"status": "restarted", "restart_count": restarted.restart_count})
        return web.json_response({"error": "Restart failed"}, status=500)
    except Exception as e:
        log.error(f"Restart endpoint error for {agent_id}: {e}")
        return web.json_response({"error": str(e)}, status=500)


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

    try:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 200))
    except ValueError:
        return web.json_response({"error": "offset and limit must be integers"}, status=400)

    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    all_lines = list(agent.output_lines)
    total = len(all_lines)
    sliced = all_lines[offset:offset + limit]

    return web.json_response({
        "data": sliced,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


async def get_agent_full_output(request: web.Request) -> web.Response:
    """Get full output: archive + live buffer combined."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 500))
    except ValueError:
        return web.json_response({"error": "offset and limit must be integers"}, status=400)

    limit = max(1, min(limit, 2000))
    offset = max(0, offset)

    # Get archived lines
    archived, archive_total = await db.get_archived_output(agent_id, offset, limit)
    live_lines = list(agent.output_lines)
    total = archive_total + len(live_lines)

    # Combine: if offset is within archive range, start from archive
    if offset < archive_total:
        remaining = limit - len(archived)
        if remaining > 0:
            combined = archived + live_lines[:remaining]
        else:
            combined = archived
    else:
        # Offset is beyond archive, read from live buffer
        live_offset = offset - archive_total
        combined = live_lines[live_offset:live_offset + limit]

    return web.json_response({
        "data": combined,
        "pagination": {"limit": limit, "offset": offset, "total": total},
        "archive_lines": archive_total,
        "live_lines": len(live_lines),
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

    # Validation rules
    validators = {
        "max_agents": lambda v: isinstance(v, int) and 1 <= v <= 100,
        "default_role": lambda v: isinstance(v, str) and v in BUILTIN_ROLES,
        "default_working_dir": lambda v: isinstance(v, str) and len(v) > 0,
        "output_capture_interval": lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 30.0,
        "memory_limit_mb": lambda v: isinstance(v, int) and 256 <= v <= 32768,
        "default_backend": lambda v: isinstance(v, str) and v in config.backends,
        "llm_enabled": lambda v: isinstance(v, bool),
        "llm_model": lambda v: isinstance(v, str) and len(v) > 0,
        "llm_summary_interval": lambda v: isinstance(v, (int, float)) and 3.0 <= v <= 120.0,
        "max_restarts": lambda v: isinstance(v, int),
        "voice_feedback": lambda v: isinstance(v, bool),
        "idle_agent_ttl": lambda v: isinstance(v, int) and 300 <= v <= 86400,
        "health_low_threshold": lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0,
        "health_critical_threshold": lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0,
        "stall_timeout_minutes": lambda v: isinstance(v, int) and 1 <= v <= 60,
        "hung_timeout_minutes": lambda v: isinstance(v, int) and 1 <= v <= 120,
    }

    # Clamp max_restarts to [1, 10] range
    if "max_restarts" in data and isinstance(data["max_restarts"], int):
        data["max_restarts"] = max(1, min(10, data["max_restarts"]))

    errors = []
    for key, value in data.items():
        if key in validators and not validators[key](value):
            errors.append(f"Invalid value for {key}: {value}")

    if errors:
        return web.json_response({"error": "; ".join(errors)}, status=400)

    allowed_keys = set(validators.keys())

    # Build YAML-safe update dict
    yaml_update = {}
    agents_keys = {"max_agents": "max_concurrent", "default_role": "default_role",
                   "default_working_dir": "default_working_dir",
                   "output_capture_interval": "output_capture_interval_sec",
                   "memory_limit_mb": "memory_limit_mb", "default_backend": "default_backend"}
    llm_keys = {"llm_enabled": "enabled", "llm_model": "model", "llm_summary_interval": "summary_interval_sec"}
    voice_keys = {"voice_feedback": "feedback_sounds"}
    alert_keys = {
        "idle_agent_ttl": "idle_ttl_seconds",
        "health_low_threshold": "health_low_threshold",
        "health_critical_threshold": "health_critical_threshold",
        "stall_timeout_minutes": "stall_timeout_minutes",
        "hung_timeout_minutes": "hung_timeout_minutes",
    }

    for key, value in data.items():
        if key not in allowed_keys:
            continue
        if key in agents_keys:
            yaml_update.setdefault("agents", {})[agents_keys[key]] = value
        elif key in llm_keys:
            yaml_update.setdefault("llm", {})[llm_keys[key]] = value
        elif key in voice_keys:
            yaml_update.setdefault("voice", {})[voice_keys[key]] = value
        elif key in alert_keys:
            yaml_update.setdefault("alerts", {})[alert_keys[key]] = value

    # FIRST: write YAML to disk. Only update in-memory config on success.
    config_path = ASHLAR_DIR / "ashlar.yaml"
    try:
        raw = DEFAULT_CONFIG.copy()
        if config_path.exists():
            with open(config_path) as f:
                raw = deep_merge(raw, yaml.safe_load(f) or {})
        raw = deep_merge(raw, yaml_update)
        # Atomic write: write to temp then rename
        tmp_path = config_path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
        tmp_path.rename(config_path)
        log.info(f"Config saved to disk: {', '.join(data.keys())}")
    except Exception as e:
        log.warning(f"Failed to save config to disk: {e}")
        # Do NOT update in-memory config — disk write failed
        return web.json_response({"error": f"Failed to save: {e}", "config": config.to_dict()}, status=500)

    # THEN: update in-memory config (disk write succeeded)
    for key in allowed_keys:
        if key in data and hasattr(config, key):
            setattr(config, key, data[key])

    # If LLM was enabled/disabled, update summarizer
    summarizer = request.app.get("llm_summarizer")
    if summarizer and "llm_enabled" in data:
        summarizer.config = config

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
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
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

    # Capacity pre-check
    config: Config = request.app["config"]
    agents_needed = len(workflow.get("agents", []))
    current_count = len(manager.agents)
    if current_count + agents_needed > config.max_agents:
        return web.json_response({
            "error": f"Not enough capacity: need {agents_needed} agents, "
                     f"but only {config.max_agents - current_count} slots available "
                     f"({current_count}/{config.max_agents} in use)",
        }, status=503)

    agent_specs = workflow.get("agents", [])
    has_deps = any(spec.get("depends_on") for spec in agent_specs)

    if has_deps:
        # DAG pipeline mode — create WorkflowRun and start with root agents
        run_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        wf_run = WorkflowRun(
            id=run_id,
            workflow_id=workflow_id,
            workflow_name=workflow["name"],
            agent_specs=agent_specs,
            pending_indices=set(range(len(agent_specs))),
            working_dir=working_dir or config.default_working_dir,
            created_at=now,
        )
        manager.workflow_runs[run_id] = wf_run

        # Spawn agents with no dependencies (root nodes)
        await manager.resolve_workflow_deps(wf_run, hub)

        await hub.broadcast_event(
            "workflow_started",
            f"Pipeline '{workflow['name']}' started (run {run_id}, {agents_needed} agents)",
            metadata={"workflow_run_id": run_id, "has_deps": True},
        )

        return web.json_response({
            "workflow_run_id": run_id,
            "workflow": workflow["name"],
            "pipeline": True,
            "agent_map": {str(k): v for k, v in wf_run.agent_map.items()},
            "pending": list(wf_run.pending_indices),
        })
    else:
        # Legacy parallel mode — spawn all at once
        agent_ids = []
        failed = []
        for agent_def in agent_specs:
            try:
                agent = await manager.spawn(
                    role=agent_def.get("role", "general"),
                    name=agent_def.get("name"),
                    working_dir=agent_def.get("working_dir") or working_dir,
                    task=agent_def.get("task", ""),
                    backend=agent_def.get("backend", config.default_backend),
                    model=agent_def.get("model"),
                    tools=agent_def.get("tools"),
                )
                agent_ids.append(agent.id)
            except ValueError as e:
                log.warning(f"Workflow spawn failed: {e}")
                failed.append({"role": agent_def.get("role", "general"), "error": str(e)})

        # Link related agents
        for aid in agent_ids:
            a = manager.agents.get(aid)
            if a:
                a.related_agents = [x for x in agent_ids if x != aid]
                await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})

        await hub.broadcast_event(
            "workflow_started",
            f"Workflow '{workflow['name']}' started ({len(agent_ids)} agents)",
        )

        result: dict[str, Any] = {"agent_ids": agent_ids, "workflow": workflow["name"]}
        if failed:
            result["spawned"] = agent_ids
            result["failed"] = failed

        return web.json_response(result)


async def list_workflow_runs(request: web.Request) -> web.Response:
    """GET /api/workflow-runs — list active and recent workflow runs."""
    manager: AgentManager = request.app["agent_manager"]
    runs = [wr.to_dict() for wr in manager.workflow_runs.values()]
    return web.json_response(runs)


async def get_workflow_run(request: web.Request) -> web.Response:
    """GET /api/workflow-runs/{id} — get details of a workflow run."""
    manager: AgentManager = request.app["agent_manager"]
    run_id = request.match_info["id"]
    wf_run = manager.workflow_runs.get(run_id)
    if not wf_run:
        return web.json_response({"error": "Workflow run not found"}, status=404)
    return web.json_response(wf_run.to_dict())


async def cancel_workflow_run(request: web.Request) -> web.Response:
    """POST /api/workflow-runs/{id}/cancel — cancel a running workflow pipeline."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    run_id = request.match_info["id"]
    wf_run = manager.workflow_runs.get(run_id)
    if not wf_run:
        return web.json_response({"error": "Workflow run not found"}, status=404)
    if wf_run.status != "running":
        return web.json_response({"error": f"Workflow is already {wf_run.status}"}, status=400)

    wf_run.status = "cancelled"
    wf_run.completed_at = datetime.now(timezone.utc).isoformat()
    wf_run.pending_indices.clear()

    # Kill running agents
    for aid in list(wf_run.running_ids):
        await manager.kill(aid)
        await hub.broadcast({"type": "agent_removed", "agent_id": aid})

    wf_run.running_ids.clear()
    await hub.broadcast_event(
        "workflow_cancelled",
        f"Pipeline '{wf_run.workflow_name}' cancelled",
        metadata={"workflow_run_id": run_id},
    )
    return web.json_response({"status": "cancelled", "workflow_run_id": run_id})


async def delete_workflow(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflow_id = request.match_info["id"]
    if workflow_id.startswith("builtin-"):
        return web.json_response({"error": "Cannot delete built-in workflows"}, status=400)
    success = await db.delete_workflow(workflow_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Workflow not found"}, status=404)


async def update_workflow(request: web.Request) -> web.Response:
    """PUT /api/workflows/{id} — update an existing workflow."""
    db: Database = request.app["db"]
    workflow_id = request.match_info["id"]

    if workflow_id.startswith("builtin-"):
        return web.json_response({"error": "Cannot edit built-in workflows"}, status=400)

    existing = await db.get_workflow(workflow_id)
    if not existing:
        return web.json_response({"error": "Workflow not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = data.get("name", existing["name"])
    description = data.get("description", existing.get("description", ""))
    agents = data.get("agents", existing.get("agents", []))

    if not name or not agents:
        return web.json_response({"error": "name and agents are required"}, status=400)

    # Validate agent roles
    for agent_spec in agents:
        if agent_spec.get("role") and agent_spec["role"] not in BUILTIN_ROLES:
            return web.json_response({"error": f"Unknown role: {agent_spec['role']}"}, status=400)

    workflow = {
        "id": workflow_id,
        "name": name,
        "description": description,
        "agents_json": agents,
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
    }
    await db.save_workflow(workflow)

    return web.json_response({"id": workflow_id, "name": name, "description": description, "agents": agents})


# ── Backend endpoints ──

async def list_backends(request: web.Request) -> web.Response:
    """GET /api/backends — list available backends with rich capability info."""
    manager: AgentManager = request.app["agent_manager"]
    result = {}
    for name, bc in manager.backend_configs.items():
        d = bc.to_dict()
        d["name"] = name
        result[name] = d
    return web.json_response(result)


# ── Agent Messaging endpoints ──

async def send_agent_message(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/message — send message from one agent to another."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    manager: AgentManager = request.app["agent_manager"]
    from_agent_id = request.match_info["id"]

    from_agent = manager.agents.get(from_agent_id)
    if not from_agent:
        return web.json_response({"error": "Sender agent not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_agent_id = data.get("to_agent_id")
    content = data.get("content", "")
    if not to_agent_id or not content:
        return web.json_response({"error": "to_agent_id and content required"}, status=400)

    to_agent = manager.agents.get(to_agent_id)
    if not to_agent:
        return web.json_response({"error": "Target agent not found"}, status=404)

    msg = {
        "id": uuid.uuid4().hex[:8],
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.save_message(msg)
    to_agent.unread_messages += 1

    # Send to tmux session
    sanitized = content.strip()[:500]
    sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)  # Strip control chars except newline/tab
    await manager.send_message(to_agent_id, f"[Message from {from_agent.name}]: {sanitized}")

    await hub.broadcast({"type": "agent_message", "message": msg})
    await hub.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})

    return web.json_response(msg, status=201)


async def get_agent_messages(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/messages — get messages for an agent."""
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)

    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    messages = await db.get_messages_for_agent(agent_id, limit)
    total = await db.get_message_count_for_agent(agent_id)

    # Mark as read
    read_count = await db.mark_messages_read(agent_id)
    if read_count > 0:
        agent.unread_messages = 0
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

    return web.json_response({
        "data": messages,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


# ── File Conflicts endpoint ──

async def list_conflicts(request: web.Request) -> web.Response:
    """GET /api/conflicts — list current file conflicts between agents."""
    manager: AgentManager = request.app["agent_manager"]
    conflicts = []
    for file_path, agent_ids in manager.file_activity.items():
        active_ids = [
            aid for aid in agent_ids
            if aid in manager.agents and manager.agents[aid].status in ("working", "planning", "reading")
        ]
        if len(active_ids) > 1:
            agents_info = [
                {"id": aid, "name": manager.agents[aid].name, "role": manager.agents[aid].role}
                for aid in active_ids
            ]
            conflicts.append({"file_path": file_path, "agents": agents_info})
    return web.json_response(conflicts)


# ── Agent Handoff endpoint ──

async def handoff_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{from_id}/handoff — structured handoff from one agent to another."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    manager: AgentManager = request.app["agent_manager"]
    from_id = request.match_info["id"]

    from_agent = manager.agents.get(from_id)
    if not from_agent:
        return web.json_response({"error": "Source agent not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_id = data.get("to_agent_id")
    if not to_id:
        return web.json_response({"error": "to_agent_id is required"}, status=400)

    to_agent = manager.agents.get(to_id)
    if not to_agent:
        return web.json_response({"error": "Target agent not found"}, status=404)

    key_findings = data.get("key_findings", "")
    files_modified = data.get("files_modified", [])

    # Build handoff block
    from_role = BUILTIN_ROLES.get(from_agent.role, BUILTIN_ROLES["general"])
    handoff_block = (
        f"[HANDOFF from {from_agent.name} ({from_role.name})]:\n"
        f"Summary: {from_agent.summary}\n"
    )
    if key_findings:
        handoff_block += f"Key findings: {key_findings}\n"
    if files_modified:
        handoff_block += f"Files modified: {', '.join(files_modified)}\n"
    # Add last 20 lines of output as context
    recent = list(from_agent.output_lines)[-20:]
    if recent:
        handoff_block += f"Recent output:\n" + "\n".join(recent)

    # Save as structured message
    msg = {
        "id": uuid.uuid4().hex[:8],
        "from_agent_id": from_id,
        "to_agent_id": to_id,
        "content": handoff_block,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.save_message(msg)
    to_agent.unread_messages += 1

    # Send to target agent's tmux session
    await manager.send_message(to_id, handoff_block[:2000])

    # Auto-write handoff data to scratchpad if agent has a project
    if from_agent.project_id:
        if key_findings:
            await db.upsert_scratchpad(from_agent.project_id, f"handoff_{from_agent.name}_findings", key_findings, from_agent.name)
        if files_modified:
            await db.upsert_scratchpad(from_agent.project_id, f"handoff_{from_agent.name}_files", ", ".join(files_modified), from_agent.name)

    await hub.broadcast({"type": "agent_message", "message": msg})
    await hub.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})
    await hub.broadcast_event(
        "agent_handoff",
        f"Handoff: {from_agent.name} → {to_agent.name}",
        from_id, from_agent.name,
        {"to_agent_id": to_id, "to_agent_name": to_agent.name},
    )

    return web.json_response({"status": "handed_off", "message_id": msg["id"]})


# ── LLM Summary endpoint ──

async def generate_summary(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/summarize — manually trigger LLM summary."""
    manager: AgentManager = request.app["agent_manager"]
    summarizer: LLMSummarizer | None = request.app.get("llm_summarizer")
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)

    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if not summarizer:
        return web.json_response({"error": "LLM not configured"}, status=503)

    summary = await summarizer.summarize(
        list(agent.output_lines), agent.task, agent.role, agent.status
    )
    if summary:
        agent.summary = summary
        agent._llm_summary = summary
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"summary": summary})
    return web.json_response({"error": "LLM summary generation failed", "fallback": agent.summary}, status=503)


# ── Cost tracking endpoint ──

async def get_costs(request: web.Request) -> web.Response:
    """GET /api/costs — aggregate cost estimates across active and historical agents."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]

    # Active agents
    active_cost = 0.0
    active_tokens_in = 0
    active_tokens_out = 0
    for agent in manager.agents.values():
        active_cost += agent.estimated_cost_usd
        active_tokens_in += agent.tokens_input
        active_tokens_out += agent.tokens_output

    # Historical (from DB)
    history = await db.get_agent_history(limit=200)
    hist_cost = sum(h.get("estimated_cost_usd", 0) or 0 for h in history)
    hist_tokens_in = sum(h.get("tokens_input", 0) or 0 for h in history)
    hist_tokens_out = sum(h.get("tokens_output", 0) or 0 for h in history)

    return web.json_response({
        "active": {
            "cost_usd": round(active_cost, 4),
            "tokens_input": active_tokens_in,
            "tokens_output": active_tokens_out,
            "agent_count": len(manager.agents),
        },
        "historical": {
            "cost_usd": round(hist_cost, 4),
            "tokens_input": hist_tokens_in,
            "tokens_output": hist_tokens_out,
        },
        "total_cost_usd": round(active_cost + hist_cost, 4),
    })


# ── Agent Preset endpoints ──

async def list_presets(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    presets = await db.get_presets()
    return web.json_response(presets)


async def create_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data.get("name"):
        return web.json_response({"error": "name is required"}, status=400)

    role = data.get("role", "general")
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Invalid role '{role}'. Valid roles: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)
    backend = data.get("backend", "claude-code")
    if backend not in KNOWN_BACKENDS:
        return web.json_response({"error": f"Invalid backend '{backend}'. Valid backends: {', '.join(KNOWN_BACKENDS.keys())}"}, status=400)

    preset = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"][:100],
        "role": role,
        "backend": backend,
        "task": data.get("task", "")[:10000],
        "system_prompt": data.get("system_prompt", "")[:5000],
        "model": data.get("model", ""),
        "tools_allowed": data.get("tools_allowed", ""),
        "working_dir": data.get("working_dir", ""),
    }
    try:
        await db.save_preset(preset)
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": f"Preset '{preset['name']}' already exists"}, status=409)
        raise
    return web.json_response(preset, status=201)


async def update_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    preset_id = request.match_info["id"]
    existing = await db.get_preset(preset_id)
    if not existing:
        return web.json_response({"error": "Preset not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updated = {**existing}
    for key in ("name", "role", "backend", "task", "system_prompt", "model", "tools_allowed", "working_dir"):
        if key in data:
            updated[key] = data[key]
    updated["name"] = updated["name"][:100]
    updated["task"] = updated["task"][:10000]

    if updated.get("role") and updated["role"] not in BUILTIN_ROLES:
        return web.json_response({"error": f"Invalid role '{updated['role']}'. Valid roles: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)
    if updated.get("backend") and updated["backend"] not in KNOWN_BACKENDS:
        return web.json_response({"error": f"Invalid backend '{updated['backend']}'. Valid backends: {', '.join(KNOWN_BACKENDS.keys())}"}, status=400)

    try:
        await db.save_preset(updated)
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": f"Preset name '{updated['name']}' already exists"}, status=409)
        raise
    return web.json_response(updated)


async def delete_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    preset_id = request.match_info["id"]
    success = await db.delete_preset(preset_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Preset not found"}, status=404)


# ── Agent Clone endpoint ──

async def clone_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/clone — clone an agent's config into a new agent."""
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    source_id = request.match_info["id"]

    source = manager.agents.get(source_id)
    if not source:
        return web.json_response({"error": "Source agent not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        data = {}

    working_dir = data.get("working_dir", source.working_dir)
    name = data.get("name", f"{source.name}-clone")

    config: Config = request.app["config"]
    if len(manager.agents) >= config.max_agents:
        return web.json_response({"error": "Max agents reached"}, status=503)

    try:
        agent = await manager.spawn(
            role=source.role,
            name=name,
            working_dir=working_dir,
            task=source.task,
            backend=source.backend,
            model=source.model,
            tools=source.tools_allowed,
            system_prompt_extra=source.system_prompt,
        )
        if source.project_id:
            agent.project_id = source.project_id
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        await hub.broadcast_event(
            "agent_spawned",
            f"Cloned agent: {agent.name} (from {source.name})",
            agent.id, agent.name,
        )
        return web.json_response({"agent": agent.to_dict()}, status=201)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


# ── Scratchpad endpoints ──

async def get_scratchpad(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    project_id = request.query.get("project_id")
    if not project_id:
        return web.json_response({"error": "project_id query parameter is required"}, status=400)
    entries = await db.get_scratchpad(project_id)
    return web.json_response(entries)


async def upsert_scratchpad(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    project_id = data.get("project_id")
    key = data.get("key")
    value = data.get("value", "")
    set_by = data.get("set_by", "")

    if not project_id or not key:
        return web.json_response({"error": "project_id and key are required"}, status=400)

    await db.upsert_scratchpad(project_id, key[:200], value[:10000], set_by[:100])
    return web.json_response({"status": "ok", "key": key})


async def delete_scratchpad_entry(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    key = request.match_info["key"]
    project_id = request.query.get("project_id")
    if not project_id:
        return web.json_response({"error": "project_id query parameter is required"}, status=400)
    success = await db.delete_scratchpad(project_id, key)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Entry not found"}, status=404)


# ── Config Import/Export endpoints ──

async def export_config(request: web.Request) -> web.Response:
    """GET /api/config/export — download full ashlar.yaml as JSON."""
    if r := _check_rate(request, cost=1):
        return r
    config_path = ASHLAR_DIR / "ashlar.yaml"
    if not config_path.exists():
        return web.json_response({"error": "Config file not found"}, status=404)
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return web.json_response(raw, headers={
            "Content-Disposition": "attachment; filename=ashlar_config.json"
        })
    except Exception as e:
        return web.json_response({"error": f"Failed to read config: {e}"}, status=500)


async def import_config(request: web.Request) -> web.Response:
    """POST /api/config/import — import config from JSON, validate, apply."""
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "Config must be a JSON object"}, status=400)

    # Read current config for diff
    config_path = ASHLAR_DIR / "ashlar.yaml"
    current = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                current = yaml.safe_load(f) or {}
        except Exception:
            pass

    # Build diff for preview
    def flat_diff(old: dict, new: dict, prefix: str = "") -> list[dict]:
        changes = []
        all_keys = set(list(old.keys()) + list(new.keys()))
        for k in sorted(all_keys):
            path = f"{prefix}.{k}" if prefix else k
            ov = old.get(k)
            nv = new.get(k)
            if isinstance(ov, dict) and isinstance(nv, dict):
                changes.extend(flat_diff(ov, nv, path))
            elif ov != nv:
                changes.append({"key": path, "old": ov, "new": nv})
        return changes

    diff = flat_diff(current, data)

    # Validate before writing — try loading the imported data as config
    try:
        tmp_validate = config_path.with_suffix(".yaml.validate")
        with open(tmp_validate, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        # Attempt to parse — load_config reads from the standard path,
        # so we validate structure manually
        test_raw = deep_merge(DEFAULT_CONFIG.copy(), data)
        # Check critical sections exist and have valid types
        if not isinstance(test_raw.get("server", {}), dict):
            raise ValueError("'server' must be an object")
        if not isinstance(test_raw.get("agents", {}), dict):
            raise ValueError("'agents' must be an object")
        tmp_validate.unlink(missing_ok=True)
    except ValueError as e:
        return web.json_response({"error": f"Invalid config structure: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Config validation failed: {e}"}, status=400)

    # Atomic write (validated)
    try:
        tmp_path = config_path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.rename(config_path)
    except Exception as e:
        return web.json_response({"error": f"Failed to write config: {e}"}, status=500)

    # Reload in-memory config
    config: Config = request.app["config"]
    try:
        reloaded = load_config(bool(shutil.which("claude")))
        for attr in ("max_agents", "default_role", "default_working_dir", "output_capture_interval",
                      "memory_limit_mb", "default_backend", "llm_enabled", "llm_model",
                      "llm_summary_interval", "voice_feedback", "idle_agent_ttl",
                      "health_low_threshold", "health_critical_threshold",
                      "stall_timeout_minutes", "hung_timeout_minutes"):
            if hasattr(reloaded, attr):
                setattr(config, attr, getattr(reloaded, attr))
    except Exception as e:
        log.warning(f"Config reload partial failure: {e}")

    return web.json_response({"status": "imported", "changes": diff})


# ── History endpoints ──

async def list_history(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)
    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    history = await db.get_agent_history(limit, offset)
    total = await db.get_agent_history_count()
    return web.json_response({
        "data": history,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


async def get_history_item(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    item = await db.get_agent_history_item(agent_id)
    if not item:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(item)


# ── Activity Events endpoint ──

async def list_events(request: web.Request) -> web.Response:
    """GET /api/events — list activity events with optional filters."""
    db: Database = request.app["db"]
    try:
        limit = int(request.query.get("limit", 100))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    agent_id = request.query.get("agent_id")
    event_type = request.query.get("event_type")
    since = request.query.get("since")
    events = await db.get_events(limit, offset, agent_id, event_type, since)
    total = await db.get_events_count(agent_id, event_type, since)
    return web.json_response({
        "data": events,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


# ── Agent PATCH endpoint (Wave 3A) ──

async def patch_agent(request: web.Request) -> web.Response:
    """PATCH /api/agents/{id} — update agent fields (name, task, project_id)."""
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

    if not data:
        return web.json_response({"error": "No fields to update"}, status=400)

    errors = []

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str):
            errors.append("name must be a string")
        else:
            name = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
            if not name:
                errors.append("name cannot be empty")
            else:
                agent.name = name

    if "task" in data:
        task = data["task"]
        if not isinstance(task, str):
            errors.append("task must be a string")
        elif len(task) > 10000:
            errors.append("task exceeds 10000 character limit")
        else:
            agent.task = task

    if "project_id" in data:
        project_id = data["project_id"]
        if project_id is not None and not isinstance(project_id, str):
            errors.append("project_id must be a string or null")
        else:
            agent.project_id = project_id

    if errors:
        return web.json_response({"error": "; ".join(errors)}, status=400)

    agent.updated_at = datetime.now(timezone.utc).isoformat()
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

    return web.json_response(agent.to_dict())


# ── Bulk operations endpoint (Wave 3B) ──

async def bulk_agent_action(request: web.Request) -> web.Response:
    """POST /api/agents/bulk — perform bulk kill/pause/resume on multiple agents."""
    if r := _check_rate(request, cost=2, rate=1.0, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    action = data.get("action")
    agent_ids = data.get("agent_ids", [])

    if action not in ("kill", "pause", "resume"):
        return web.json_response({"error": f"Invalid action '{action}'. Must be 'kill', 'pause', or 'resume'"}, status=400)

    if not isinstance(agent_ids, list) or not agent_ids:
        return web.json_response({"error": "agent_ids must be a non-empty list"}, status=400)

    success_ids = []
    failed_items = []

    for aid in agent_ids:
        if not isinstance(aid, str):
            failed_items.append({"id": str(aid), "error": "Invalid agent ID type"})
            continue

        agent = manager.agents.get(aid)
        if not agent:
            failed_items.append({"id": aid, "error": "Agent not found"})
            continue

        try:
            if action == "kill":
                # Archive to history before killing
                try:
                    await db.save_agent(agent)
                except Exception as e:
                    log.warning(f"Failed to archive agent {aid} during bulk kill: {e}")
                name = agent.name
                ok = await manager.kill(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_removed", "agent_id": aid})
                    await hub.broadcast_event("agent_killed", f"Agent {name} killed (bulk)", aid, name)
                else:
                    failed_items.append({"id": aid, "error": "Kill failed"})

            elif action == "pause":
                ok = await manager.pause(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Pause failed"})

            elif action == "resume":
                ok = await manager.resume(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Resume failed"})

        except Exception as e:
            failed_items.append({"id": aid, "error": str(e)})

    return web.json_response({"success": success_ids, "failed": failed_items})


async def bulk_respond(request: web.Request) -> web.Response:
    """Send responses to multiple waiting agents at once."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    responses = data.get("responses", [])
    if not responses or not isinstance(responses, list):
        return web.json_response({"error": "responses must be a non-empty list"}, status=400)

    success_ids = []
    failed_items = []

    for item in responses:
        aid = item.get("agent_id", "")
        message = item.get("message", "")
        if not aid or not message:
            failed_items.append({"id": aid, "error": "Missing agent_id or message"})
            continue

        agent = manager.agents.get(aid)
        if not agent:
            failed_items.append({"id": aid, "error": "Agent not found"})
            continue

        # Sanitize message
        message = message[:500].replace('\r', '')
        message = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', message)

        try:
            await manager._tmux_send_keys(agent.tmux_session, message)
            agent.needs_input = False
            agent.input_prompt = None
            agent.updated_at = datetime.now(timezone.utc).isoformat()
            success_ids.append(aid)
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        except Exception as e:
            failed_items.append({"id": aid, "error": str(e)})

    return web.json_response({"success": success_ids, "failed": failed_items})


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
            # Phase 1: Parallel output capture
            active_agents = [
                (aid, agent) for aid, agent in list(manager.agents.items())
                if agent.status != "paused"
            ]

            # Check spawn timeouts first (cheap, no I/O)
            for agent_id, agent in active_agents:
                if agent.status == "spawning" and agent._spawn_time > 0:
                    if time.monotonic() - agent._spawn_time > 30:
                        agent.status = "error"
                        agent.error_message = "Spawn timeout — no output after 30s"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_error", f"Agent {agent.name} spawn timed out", agent_id, agent.name)

            # Parallel capture — all tmux reads happen concurrently
            async def _capture_one(aid: str) -> tuple[str, list[str] | None]:
                try:
                    lines = await manager.capture_output(aid)
                    return (aid, lines)
                except Exception as e:
                    log.warning(f"Output capture error for {aid}: {e}")
                    return (aid, None)

            capture_tasks = [_capture_one(aid) for aid, agent in active_agents if agent.status != "error"]
            results = await asyncio.gather(*capture_tasks, return_exceptions=True)

            # Phase 2: Sequential processing of capture results
            for result in results:
                if isinstance(result, Exception):
                    log.warning(f"Capture task exception: {result}")
                    continue
                agent_id, new_lines = result
                agent = manager.agents.get(agent_id)
                if not agent:
                    continue

                if new_lines is None:
                    # Capture failed — mark error
                    agent.status = "error"
                    agent.error_message = "Output capture failed"
                    agent._error_entered_at = time.monotonic()
                    agent.updated_at = datetime.now(timezone.utc).isoformat()
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    continue

                if new_lines:
                    now_mono = time.monotonic()

                    # -- Per-agent metrics tracking --
                    if not agent._first_output_received and agent._spawn_time > 0:
                        agent._first_output_received = True
                        agent.time_to_first_output = round(now_mono - agent._spawn_time, 2)

                    agent.last_output_time = now_mono
                    line_count = len(new_lines)
                    agent.total_output_lines += line_count
                    agent._output_line_timestamps.append((now_mono, line_count))

                    # Archive overflow lines to DB if flagged by capture_output
                    if hasattr(agent, '_overflow_to_archive') and agent._overflow_to_archive:
                        aid_arch, overflow_lines, offset = agent._overflow_to_archive
                        agent._overflow_to_archive = None
                        if app.get("db_available", True):
                            db: Database = app["db"]
                            try:
                                await db.archive_output(aid_arch, overflow_lines, offset)
                            except Exception as e:
                                log.debug(f"Output archiving failed for {agent_id}: {e}")

                    # Rolling output rate
                    cutoff = now_mono - 60.0
                    recent_lines_count = sum(
                        count for ts, count in agent._output_line_timestamps if ts >= cutoff
                    )
                    window = min(60.0, now_mono - agent._spawn_time) if agent._spawn_time > 0 else 60.0
                    agent.output_rate = (recent_lines_count / max(window, 1.0)) * 60.0

                    await hub.broadcast({
                        "type": "agent_output",
                        "agent_id": agent_id,
                        "lines": new_lines,
                    })

                    # File conflict detection
                    try:
                        conflicts = manager._check_file_conflicts(agent_id, new_lines)
                        for conflict in conflicts:
                            await hub.broadcast_event(
                                "file_conflict",
                                f"File conflict: {conflict['agent_name']} and {conflict['other_agent_name']} both working on {conflict['file_path']}",
                                agent_id, agent.name,
                                conflict,
                            )
                    except Exception:
                        pass

                    # Update summary
                    agent.summary = extract_summary(list(agent.output_lines), agent.task)
                    agent.progress_pct = estimate_progress(agent)

                    # LLM summary with throttling
                    summarizer: LLMSummarizer | None = app.get("llm_summarizer")
                    if summarizer and app["config"].llm_enabled:
                        if now_mono - agent._last_llm_summary_time >= app["config"].llm_summary_interval:
                            agent._last_llm_summary_time = now_mono
                            try:
                                llm_summary = await summarizer.summarize(
                                    list(agent.output_lines), agent.task,
                                    agent.role, agent.status
                                )
                                if llm_summary:
                                    agent.summary = llm_summary
                                    agent._llm_summary = llm_summary
                            except Exception as e:
                                log.debug(f"LLM summary failed for {agent_id}: {e}")

                # Output staleness detection (runs whether or not there were new lines)
                if agent.status == "working" and agent.last_output_time > 0:
                    silence = time.monotonic() - agent.last_output_time
                    if silence > 900:
                        agent.status = "error"
                        agent.error_message = "No output for 15 minutes"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_stale", f"Agent {agent.name} stale — no output for 15 minutes", agent_id, agent.name)
                    elif silence > 300:
                        await hub.broadcast_event("agent_stale_warning", f"Agent {agent.name} has had no output for {int(silence)}s", agent_id, agent.name)

                # Health score
                try:
                    agent.health_score = calculate_health_score(agent, app["config"].memory_limit_mb)
                except Exception:
                    pass

                # Health-driven events (using configurable thresholds)
                _crit_thresh = app["config"].health_critical_threshold
                _low_thresh = app["config"].health_low_threshold
                if agent.health_score < _crit_thresh:
                    if not getattr(agent, '_health_critical_warned', False):
                        agent._health_critical_warned = True
                        await hub.broadcast_event(
                            "agent_health_critical",
                            f"Agent {agent.name} health critical ({int(agent.health_score * 100)}%) — consider restarting",
                            agent_id, agent.name,
                        )
                elif agent.health_score < _low_thresh:
                    if not getattr(agent, '_health_low_warned', False):
                        agent._health_low_warned = True
                        await hub.broadcast_event(
                            "agent_health_low",
                            f"Agent {agent.name} health low ({int(agent.health_score * 100)}%)",
                            agent_id, agent.name,
                        )
                elif agent.health_score >= 0.5:
                    # Reset warning flags when health recovers
                    agent._health_low_warned = False
                    agent._health_critical_warned = False

                # Detect status
                try:
                    new_status = await manager.detect_status(agent_id)
                    if new_status != agent.status:
                        old_status = agent.status
                        agent.status = new_status
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        log.debug(f"Agent {agent_id} status: {old_status} -> {new_status}")

                        if new_status == "error" and old_status != "error":
                            agent._error_entered_at = time.monotonic()
                            wf_run, failure_action = manager.on_agent_failed(agent_id)
                            if wf_run:
                                if failure_action == "retry":
                                    await hub.broadcast_event(
                                        "workflow_agent_retry",
                                        f"Pipeline: retrying agent {agent.name}",
                                        agent_id, agent.name,
                                        {"workflow_run_id": wf_run.id},
                                    )
                                elif failure_action == "skip":
                                    await hub.broadcast_event(
                                        "workflow_agent_skipped",
                                        f"Pipeline: skipping failed agent {agent.name} (on_failure=skip)",
                                        agent_id, agent.name,
                                        {"workflow_run_id": wf_run.id},
                                    )
                                await manager.resolve_workflow_deps(wf_run, hub)

                        if new_status == "idle" and old_status != "idle":
                            wf_run = manager.on_agent_complete(agent_id)
                            if wf_run:
                                await manager.resolve_workflow_deps(wf_run, hub)

                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

                        if agent.needs_input:
                            now_mono = time.monotonic()
                            if now_mono - agent._last_needs_input_event > 5.0:
                                agent._last_needs_input_event = now_mono
                                await hub.broadcast_event(
                                    "agent_needs_input",
                                    agent.input_prompt or "Agent needs input",
                                    agent_id, agent.name,
                                )
                    else:
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
    """Verify tmux sessions are alive, clean up dead agents, auto-restart crashed agents."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    _tick = 0

    while True:
        _tick += 1
        # Periodic rate limiter cleanup (~every 300s at 5s interval)
        if _tick % 60 == 0:
            rl: RateLimiter | None = app.get("rate_limiter")
            if rl:
                rl.cleanup_stale()
        try:
            for agent_id, agent in list(manager.agents.items()):
                # -- Auto-restart logic for agents in error state --
                if agent.status == "error":
                    now = time.monotonic()
                    error_duration = now - agent._error_entered_at if agent._error_entered_at > 0 else 0

                    # Only attempt restart if error has persisted >10s
                    if error_duration > 10.0 and agent.restart_count < agent.max_restarts:
                        # Exponential backoff: 5s * 2^restart_count (5s, 10s, 20s)
                        backoff = 5.0 * (2 ** agent.restart_count)
                        time_since_last_restart = now - agent.last_restart_time if agent.last_restart_time > 0 else float("inf")

                        if time_since_last_restart >= backoff:
                            log.info(
                                f"Auto-restarting agent {agent_id} ({agent.name}), "
                                f"attempt {agent.restart_count + 1}/{agent.max_restarts}, "
                                f"backoff was {backoff:.0f}s"
                            )
                            try:
                                success = await manager.restart(agent_id)
                                if success:
                                    restarted_agent = manager.agents.get(agent_id)
                                    if restarted_agent:
                                        await hub.broadcast({"type": "agent_update", "agent": restarted_agent.to_dict()})
                                        await hub.broadcast_event("agent_restarted", f"Agent {agent.name} auto-restarted (attempt {restarted_agent.restart_count})", agent_id, agent.name)
                                else:
                                    log.warning(f"Auto-restart failed for agent {agent_id}")
                            except Exception as e:
                                log.error(f"Auto-restart error for {agent_id}: {e}")

                    elif agent.restart_count >= agent.max_restarts and agent._error_entered_at > 0:
                        # Max restarts exhausted — send notification once
                        # Use _error_entered_at as a flag: set to 0 after notification
                        agent._error_entered_at = 0
                        log.warning(
                            f"Agent {agent_id} ({agent.name}) exceeded max restarts "
                            f"({agent.max_restarts}), leaving in error state"
                        )
                        await hub.broadcast_event(
                            "agent_restart_exhausted",
                            f"Agent {agent.name} failed after {agent.max_restarts} restart attempts. Manual intervention required.",
                            agent_id, agent.name,
                        )
                    continue

                # -- Check if tmux session is still alive --
                exists = await manager._tmux_session_exists(agent.tmux_session)
                if not exists:
                    log.warning(f"Agent {agent_id} ({agent.name}) tmux session died")
                    agent.status = "error"
                    agent.error_message = "tmux session terminated unexpectedly"
                    agent._error_entered_at = time.monotonic()
                    agent.updated_at = datetime.now(timezone.utc).isoformat()
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast_event("agent_error", f"Agent {agent.name} died unexpectedly", agent_id, agent.name)
                elif agent.pid:
                    # PID liveness check: verify the process is still alive
                    try:
                        os.kill(agent.pid, 0)
                    except ProcessLookupError:
                        # PID is dead but tmux session still exists
                        log.warning(f"Agent {agent_id} ({agent.name}) PID {agent.pid} is dead but tmux session alive")
                        agent.status = "error"
                        agent.error_message = f"Agent process (PID {agent.pid}) died unexpectedly"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_error", f"Agent {agent.name} process died (PID {agent.pid})", agent_id, agent.name)
                    except PermissionError:
                        pass  # Process exists but we can't signal it — that's fine

                # -- Idle agent reaping --
                if agent.status in ("idle", "complete") and agent.last_output_time > 0:
                    idle_duration = time.monotonic() - agent.last_output_time
                    idle_ttl = app["config"].idle_agent_ttl
                    if idle_ttl > 0 and idle_duration > idle_ttl:
                        log.info(f"Reaping idle agent {agent_id} ({agent.name}) — idle for {int(idle_duration)}s")
                        # Archive to history
                        db: Database = app["db"]
                        try:
                            await db.save_agent(agent)
                        except Exception as e:
                            log.warning(f"Failed to archive agent {agent_id} before reaping: {e}")
                        name = agent.name
                        await manager.kill(agent_id)
                        await hub.broadcast({"type": "agent_removed", "agent_id": agent_id, "reason": "idle_timeout"})
                        await hub.broadcast_event("agent_reaped", f"Agent {name} reaped after {int(idle_duration)}s idle", agent_id, name)

                # -- Workflow stage stall detection --
                if agent.workflow_run_id and agent.status == "paused":
                    pause_duration = time.monotonic() - agent.last_output_time if agent.last_output_time > 0 else 0
                    if pause_duration > app["config"].stall_timeout_minutes * 60:
                        if not getattr(agent, '_workflow_stall_warned', False):
                            agent._workflow_stall_warned = True
                            await hub.broadcast_event(
                                "workflow_stage_stalled",
                                f"Workflow agent {agent.name} paused for {int(pause_duration)}s — blocking downstream",
                                agent_id, agent.name,
                            )

                if agent.workflow_run_id and agent.status == "working" and agent.last_output_time > 0:
                    wf_silence = time.monotonic() - agent.last_output_time
                    if wf_silence > app["config"].hung_timeout_minutes * 60:
                        if not getattr(agent, '_workflow_hung_warned', False):
                            agent._workflow_hung_warned = True
                            await hub.broadcast_event(
                                "workflow_stage_hung",
                                f"Workflow agent {agent.name} has no output for {int(wf_silence)}s — may be hung",
                                agent_id, agent.name,
                            )
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
                    await hub.broadcast({"type": "agent_removed", "agent_id": agent_id})
                    await hub.broadcast_event("agent_killed", f"Agent {name} killed: memory limit exceeded ({agent.memory_mb}MB)", agent_id, name)
                elif agent.memory_mb > warn_threshold:
                    log.warning(f"Agent {agent_id} memory warning: {agent.memory_mb}MB / {limit}MB")
        except Exception as e:
            log.error(f"Memory watchdog error: {e}")

        await asyncio.sleep(10.0)


# ─────────────────────────────────────────────
# Section 10: Application Setup & Main
# ─────────────────────────────────────────────

async def start_background_tasks(app: web.Application) -> None:
    # Initialize database with retry
    db: Database = app["db"]
    try:
        await db.init()
    except Exception as e:
        log.error(f"Database init failed: {e}, retrying...")
        await asyncio.sleep(1)
        try:
            await db.init()
        except Exception as e2:
            log.error(f"Database init failed on retry: {e2} — running in degraded mode (no persistence)")
            app["db_available"] = False

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

    # Close LLM summarizer
    summarizer: LLMSummarizer | None = app.get("llm_summarizer")
    if summarizer:
        await summarizer.close()

    # Close database
    await db.close()

    # Clean up all tmux sessions
    manager.cleanup_all()
    log.info("Cleanup complete")


def create_app(config: Config) -> web.Application:
    middlewares = []
    if config.require_auth:
        middlewares.append(auth_middleware)
    app = web.Application(middlewares=middlewares)
    app["config"] = config

    manager = AgentManager(config)
    app["agent_manager"] = manager

    db = Database()
    app["db"] = db
    app["db_available"] = True  # Set to False if DB init fails in start_background_tasks

    hub = WebSocketHub(manager, config, db)
    app["ws_hub"] = hub

    # Rate limiter
    rate_limiter = RateLimiter()
    app["rate_limiter"] = rate_limiter

    # LLM Summarizer
    summarizer = LLMSummarizer(config)
    app["llm_summarizer"] = summarizer
    if config.llm_enabled:
        log.info(f"LLM summaries enabled: {config.llm_provider}/{config.llm_model}")
    else:
        log.info("LLM summaries disabled (set XAI_API_KEY and llm.enabled in config)")

    # Routes
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/logo.png", serve_logo)
    app.router.add_get("/ws", hub.handle_ws)

    # REST API — Agents (bulk + patch BEFORE {id} catch-all routes)
    app.router.add_get("/api/agents", list_agents)
    app.router.add_post("/api/agents", spawn_agent)
    app.router.add_post("/api/agents/bulk", bulk_agent_action)
    app.router.add_post("/api/agents/bulk-respond", bulk_respond)
    app.router.add_patch("/api/agents/{id}", patch_agent)
    app.router.add_get("/api/agents/{id}", get_agent)
    app.router.add_delete("/api/agents/{id}", delete_agent)
    app.router.add_post("/api/agents/{id}/send", send_to_agent)
    app.router.add_post("/api/agents/{id}/pause", pause_agent)
    app.router.add_post("/api/agents/{id}/resume", resume_agent)
    app.router.add_post("/api/agents/{id}/restart", restart_agent)
    app.router.add_get("/api/agents/{id}/output", get_agent_output)
    app.router.add_get("/api/agents/{id}/full-output", get_agent_full_output)
    app.router.add_post("/api/agents/{id}/summarize", generate_summary)
    app.router.add_post("/api/agents/{id}/message", send_agent_message)
    app.router.add_get("/api/agents/{id}/messages", get_agent_messages)
    app.router.add_post("/api/agents/{id}/handoff", handoff_agent)

    # REST API — System
    app.router.add_get("/api/system", system_metrics)
    app.router.add_get("/api/roles", list_roles)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/backends", list_backends)
    app.router.add_post("/api/auth/verify", verify_auth)
    app.router.add_get("/api/costs", get_costs)
    app.router.add_get("/api/conflicts", list_conflicts)

    # REST API — Projects
    app.router.add_get("/api/projects", list_projects)
    app.router.add_post("/api/projects", create_project)
    app.router.add_delete("/api/projects/{id}", delete_project)

    # REST API — Workflows
    app.router.add_get("/api/workflows", list_workflows)
    app.router.add_post("/api/workflows", create_workflow)
    app.router.add_put("/api/workflows/{id}", update_workflow)
    app.router.add_post("/api/workflows/{id}/run", run_workflow)
    app.router.add_delete("/api/workflows/{id}", delete_workflow)

    # REST API — Workflow Runs (pipeline tracking)
    app.router.add_get("/api/workflow-runs", list_workflow_runs)
    app.router.add_get("/api/workflow-runs/{id}", get_workflow_run)
    app.router.add_post("/api/workflow-runs/{id}/cancel", cancel_workflow_run)

    # REST API — History & Events
    app.router.add_get("/api/history", list_history)
    app.router.add_get("/api/history/{id}", get_history_item)
    app.router.add_get("/api/events", list_events)

    # REST API — Presets
    app.router.add_get("/api/presets", list_presets)
    app.router.add_post("/api/presets", create_preset)
    app.router.add_put("/api/presets/{id}", update_preset)
    app.router.add_delete("/api/presets/{id}", delete_preset)

    # REST API — Agent Clone
    app.router.add_post("/api/agents/{id}/clone", clone_agent)

    # REST API — Scratchpad
    app.router.add_get("/api/scratchpad", get_scratchpad)
    app.router.add_put("/api/scratchpad", upsert_scratchpad)
    app.router.add_delete("/api/scratchpad/{key}", delete_scratchpad_entry)

    # REST API — Config Import/Export
    app.router.add_get("/api/config/export", export_config)
    app.router.add_post("/api/config/import", import_config)

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
