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
import logging.handlers
import os
import re
import shlex
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

    # File handler with rotation (10 MB max, 5 backups)
    fh = logging.handlers.RotatingFileHandler(
        ASHLAR_DIR / "ashlar.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
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
        try:
            result = subprocess.run(
                ["claude", "--version"], capture_output=True, timeout=5, text=True
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                version = lines[0][:80] if lines else "unknown"
                log.info(f"claude CLI validated: {version}")
                return True
            else:
                log.warning(f"claude CLI found but --version failed (exit {result.returncode}) — using demo mode")
                return False
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning(f"claude CLI found but not functional ({e}) — using demo mode")
            return False
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
        "default_working_dir": os.getcwd(),
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
    "intelligence": {
        "enabled": True,
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-20250514",
        "summary_interval_sec": 15,
        "meta_interval_sec": 30,
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
    # Anthropic Intelligence config
    intelligence_enabled: bool = True
    intelligence_api_key: str = ""
    intelligence_model: str = "claude-sonnet-4-20250514"
    intelligence_summary_interval: float = 15.0
    intelligence_meta_interval: float = 30.0
    # Cost budget
    cost_budget_usd: float = 0.0  # 0 = no limit
    cost_budget_auto_pause: bool = False

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
            "intelligence_enabled": self.intelligence_enabled,
            "intelligence_model": self.intelligence_model,
            "cost_budget_usd": self.cost_budget_usd,
            "cost_budget_auto_pause": self.cost_budget_auto_pause,
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

    default_wd = agents.get("default_working_dir", os.getcwd())
    default_wd = os.path.expanduser(default_wd)
    if not os.path.isdir(default_wd):
        log.warning(f"default_working_dir {default_wd!r} does not exist, using server CWD")
        default_wd = os.getcwd()

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

    # Resolve Anthropic intelligence API key
    intel = raw.get("intelligence", {})
    intel_api_key_env = intel.get("api_key_env", "ANTHROPIC_API_KEY")
    intel_api_key = os.environ.get(intel_api_key_env, "")

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
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix='.yaml.tmp')
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
                Path(tmp_path).rename(config_path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception:
            pass

    # ASHLAR_PORT env var overrides config file
    port_raw = os.environ.get("ASHLAR_PORT")
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            log.warning(f"Invalid ASHLAR_PORT '{port_raw}', using default")
            port = server.get("port", 5000)
    else:
        port = server.get("port", 5000)

    return Config(
        host=server.get("host", "127.0.0.1"),
        port=port,
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
        intelligence_enabled=intel.get("enabled", True) and bool(intel_api_key),
        intelligence_api_key=intel_api_key,
        intelligence_model=intel.get("model", "claude-sonnet-4-20250514"),
        intelligence_summary_interval=intel.get("summary_interval_sec", 15.0),
        intelligence_meta_interval=intel.get("meta_interval_sec", 30.0),
        cost_budget_usd=float(raw.get("cost_budget_usd", 0)),
        cost_budget_auto_pause=bool(raw.get("cost_budget_auto_pause", False)),
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
    supports_prompt_arg: bool = False  # If True, task is passed as CLI positional arg (not via send-keys)
    # Automation
    auto_approve_flag: str = ""
    plan_mode_flag: str = ""  # CLI flag to enable plan/review mode (e.g. "--permission-mode plan")
    inject_role_prompt: bool = True  # Whether to inject role system prompt (disable for tools with their own context)
    # Status detection overrides (merge with defaults)
    status_patterns: dict[str, list[str]] = field(default_factory=dict)
    # Cost rates (per 1K tokens)
    cost_input_per_1k: float = 0.003
    cost_output_per_1k: float = 0.015
    # Context window sizing
    context_window: int = 200_000  # tokens
    char_to_token_ratio: float = 3.5

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
            "supports_prompt_arg": self.supports_prompt_arg,
            "auto_approve_flag": self.auto_approve_flag,
            "plan_mode_flag": self.plan_mode_flag,
            "inject_role_prompt": self.inject_role_prompt,
            "cost_input_per_1k": self.cost_input_per_1k,
            "cost_output_per_1k": self.cost_output_per_1k,
            "context_window": self.context_window,
            "char_to_token_ratio": self.char_to_token_ratio,
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
        supports_prompt_arg=True,
        auto_approve_flag="--dangerously-skip-permissions",
        plan_mode_flag="--permission-mode plan",
        status_patterns={
            "working": [r"⎿", r"╭─", r"╰─", r"Tool Use:", r"Bash:", r"\$ ",
                        r"esc to interrupt", r"Running ", r"Updated \d+ file",
                        r"Created ", r"Wrote ", r"tokens remaining",
                        r"Reading file", r"Editing file", r"Writing file",
                        r"Searching for", r"Globbing", r"Grepping",
                        r"npm (run|install|test)", r"pip install",
                        r"pytest", r"jest", r"vitest", r"mocha",
                        r"compiling|building|bundling",
                        r"git (add|commit|push|pull|diff|log|status)"],
            "waiting": [r"Do you want to proceed", r"Allow once", r"Allow always",
                        r"Press Enter to retry", r"Type your response",
                        r"waiting for your", r"What would you like",
                        r"Approve", r"Deny", r"Skip",
                        r"Y/n\]", r"y/N\]",
                        r"Enter a value", r"Select an option",
                        r"Choose (a|an|one|which)"],
            "planning": [r"Thinking\.\.\.", r"Schlepping", r"I'll start by",
                         r"Let me analyze", r"Let me plan",
                         r"I need to", r"First,? I('ll| will)",
                         r"Analyzing", r"Understanding"],
            "complete": [r"❯", r"Task completed", r"All done",
                         r"Successfully completed", r"Finished"],
        },
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=200_000,
        char_to_token_ratio=3.5,
    ),
    "codex": BackendConfig(
        command="codex",
        args=[],
        supports_json_output=True,
        auto_approve_flag="--full-auto",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.012,
        context_window=128_000,
        char_to_token_ratio=3.2,
    ),
    "aider": BackendConfig(
        command="aider",
        args=[],
        supports_model_select=True,
        auto_approve_flag="-y",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=128_000,
        char_to_token_ratio=3.5,
    ),
    "goose": BackendConfig(
        command="goose",
        args=[],
        supports_session_resume=True,
        auto_approve_flag="-y",
        cost_input_per_1k=0.003,
        cost_output_per_1k=0.015,
        context_window=200_000,
        char_to_token_ratio=3.5,
    ),
}


# ── Extension Discovery (Skills, MCP Servers, Plugins) ──

@dataclass
class SkillInfo:
    """A Claude Code slash-command skill discovered from filesystem."""
    name: str              # e.g. "commit" or "gsd/add-phase"
    description: str
    source: str            # "user" (global) or "project"
    file_path: str         # absolute path to the .md file
    argument_hint: str = ""
    allowed_tools: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "file_path": self.file_path,
            "argument_hint": self.argument_hint,
            "allowed_tools": self.allowed_tools,
        }


@dataclass
class MCPServerInfo:
    """An MCP server discovered from settings.json or .mcp.json."""
    name: str
    server_type: str       # "stdio" | "http" | "sse" | "unknown"
    url_or_command: str    # URL for http/sse, command for stdio
    source: str            # "user" (global) or project path
    args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.server_type,
            "url_or_command": self.url_or_command,
            "source": self.source,
            "args": self.args,
        }


@dataclass
class PluginInfo:
    """A Claude Code plugin discovered from settings.json."""
    name: str              # e.g. "frontend-design@claude-plugins-official"
    provider: str          # e.g. "claude-plugins-official"
    enabled: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "enabled": self.enabled,
        }


class ExtensionScanner:
    """Scans the filesystem for Claude Code skills, MCP servers, and plugins.
    Results are cached in memory and refreshed on demand."""

    def __init__(self) -> None:
        self.skills: list[SkillInfo] = []
        self.mcp_servers: list[MCPServerInfo] = []
        self.plugins: list[PluginInfo] = []
        self._scanned_at: str = ""

    def to_dict(self) -> dict:
        return {
            "skills": [s.to_dict() for s in self.skills],
            "mcp_servers": [m.to_dict() for m in self.mcp_servers],
            "plugins": [p.to_dict() for p in self.plugins],
            "scanned_at": self._scanned_at,
        }

    def scan(self, project_dirs: list[str] | None = None) -> dict:
        """Full filesystem scan. Returns to_dict() result."""
        self.skills = self._scan_skills(project_dirs or [])
        self.mcp_servers = self._scan_mcp_servers(project_dirs or [])
        self.plugins = self._scan_plugins()
        self._scanned_at = datetime.now(timezone.utc).isoformat()
        log.info(
            f"Extension scan: {len(self.skills)} skills, "
            f"{len(self.mcp_servers)} MCP servers, {len(self.plugins)} plugins"
        )
        return self.to_dict()

    def _scan_skills(self, project_dirs: list[str]) -> list[SkillInfo]:
        """Scan for .md skill files in user global + project dirs."""
        skills: list[SkillInfo] = []
        # User global: ~/.claude/commands/**/*.md
        global_dir = Path.home() / ".claude" / "commands"
        if global_dir.is_dir():
            skills.extend(self._scan_skill_dir(global_dir, "user"))
        # Per-project: {project}/.claude/commands/**/*.md
        for pdir in project_dirs:
            proj_cmd_dir = Path(pdir) / ".claude" / "commands"
            if proj_cmd_dir.is_dir():
                skills.extend(self._scan_skill_dir(proj_cmd_dir, pdir))
        return skills

    def _scan_skill_dir(self, base_dir: Path, source: str) -> list[SkillInfo]:
        """Scan a single commands directory for .md skill files."""
        results: list[SkillInfo] = []
        try:
            for md_file in sorted(base_dir.rglob("*.md")):
                if not md_file.is_file():
                    continue
                # Build skill name: relative to base_dir, without extension
                rel = md_file.relative_to(base_dir)
                name = str(rel.with_suffix(""))  # e.g. "commit" or "gsd/add-phase"
                # Parse YAML frontmatter
                desc, arg_hint, allowed = self._parse_skill_frontmatter(md_file)
                results.append(SkillInfo(
                    name=name,
                    description=desc,
                    source=source,
                    file_path=str(md_file),
                    argument_hint=arg_hint,
                    allowed_tools=allowed,
                ))
        except Exception as e:
            log.warning(f"Error scanning skills in {base_dir}: {e}")
        return results

    @staticmethod
    def _parse_skill_frontmatter(path: Path) -> tuple[str, str, str]:
        """Parse YAML frontmatter from a skill .md file.
        Returns (description, argument_hint, allowed_tools)."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ("", "", "")
        if not text.startswith("---"):
            return ("", "", "")
        end = text.find("---", 3)
        if end < 0:
            return ("", "", "")
        frontmatter = text[3:end].strip()
        try:
            meta = yaml.safe_load(frontmatter) or {}
        except Exception:
            return ("", "", "")
        desc = str(meta.get("description", ""))
        arg_hint = str(meta.get("argument-hint", ""))
        allowed = str(meta.get("allowed-tools", ""))
        return (desc, arg_hint, allowed)

    def _scan_mcp_servers(self, project_dirs: list[str]) -> list[MCPServerInfo]:
        """Scan for MCP server configurations."""
        servers: list[MCPServerInfo] = []
        # Global: ~/.claude/settings.json → mcpServers
        settings_path = Path.home() / ".claude" / "settings.json"
        if settings_path.is_file():
            servers.extend(self._parse_mcp_from_settings(settings_path, "user"))
        # Per-project: {project}/.mcp.json
        for pdir in project_dirs:
            mcp_path = Path(pdir) / ".mcp.json"
            if mcp_path.is_file():
                servers.extend(self._parse_mcp_from_file(mcp_path, pdir))
        return servers

    def _parse_mcp_from_settings(self, path: Path, source: str) -> list[MCPServerInfo]:
        """Parse mcpServers from a settings.json file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mcp_section = data.get("mcpServers", {})
            return self._parse_mcp_dict(mcp_section, source)
        except Exception as e:
            log.warning(f"Error parsing MCP from {path}: {e}")
            return []

    def _parse_mcp_from_file(self, path: Path, source: str) -> list[MCPServerInfo]:
        """Parse an .mcp.json file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mcp_section = data.get("mcpServers", data)
            return self._parse_mcp_dict(mcp_section, source)
        except Exception as e:
            log.warning(f"Error parsing MCP from {path}: {e}")
            return []

    @staticmethod
    def _parse_mcp_dict(mcp_dict: dict, source: str) -> list[MCPServerInfo]:
        """Convert a mcpServers dict to list of MCPServerInfo."""
        results: list[MCPServerInfo] = []
        if not isinstance(mcp_dict, dict):
            return results
        for name, cfg in mcp_dict.items():
            if not isinstance(cfg, dict):
                continue
            # Determine type
            stype = cfg.get("type", "unknown")
            if stype == "stdio":
                url_or_cmd = cfg.get("command", "")
                args = cfg.get("args", [])
            elif stype in ("http", "sse"):
                url_or_cmd = cfg.get("url", "")
                args = []
            else:
                url_or_cmd = cfg.get("command", cfg.get("url", ""))
                args = cfg.get("args", [])
            results.append(MCPServerInfo(
                name=name,
                server_type=stype,
                url_or_command=url_or_cmd,
                source=source,
                args=args if isinstance(args, list) else [],
            ))
        return results

    def _scan_plugins(self) -> list[PluginInfo]:
        """Scan for enabled plugins from settings.json."""
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.is_file():
            return []
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            plugins_section = data.get("enabledPlugins", {})
            if not isinstance(plugins_section, dict):
                return []
            results: list[PluginInfo] = []
            for full_name, enabled in plugins_section.items():
                # Parse "name@provider" format
                parts = full_name.split("@", 1)
                short_name = parts[0]
                provider = parts[1] if len(parts) > 1 else "unknown"
                results.append(PluginInfo(
                    name=short_name,
                    provider=provider,
                    enabled=bool(enabled),
                ))
            return results
        except Exception as e:
            log.warning(f"Error scanning plugins: {e}")
            return []


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
        key="frontend", name="Frontend Engineer", icon="paintbrush", color="#8B5CF6",
        description="React, Vue, CSS, UI/UX, accessibility",
        system_prompt="You are a frontend specialist. Focus on component architecture, responsive design, accessibility (WCAG), and performance. Prefer TypeScript, functional components, and Tailwind. Write tests for all components.",
    ),
    "backend": Role(
        key="backend", name="Backend Engineer", icon="server", color="#3B82F6",
        description="APIs, databases, Python, Node.js, auth",
        system_prompt="You are a backend specialist. Focus on API design, database schemas, auth, and error handling. Write clean, well-tested code with proper validation and logging. Prefer async patterns.",
    ),
    "devops": Role(
        key="devops", name="DevOps Engineer", icon="rocket", color="#F97316",
        description="Infrastructure, CI/CD, Docker, deployment",
        system_prompt="You are a DevOps specialist. Focus on infrastructure as code, CI/CD pipelines, containerization, monitoring, and deployment automation. Prioritize reliability and observability.",
    ),
    "tester": Role(
        key="tester", name="QA Engineer", icon="test-tube", color="#22C55E",
        description="Unit tests, integration tests, E2E, coverage",
        system_prompt="You are a QA specialist. Write comprehensive tests: unit, integration, and E2E. Aim for high coverage on critical paths. Test edge cases, error conditions, and race conditions.",
    ),
    "reviewer": Role(
        key="reviewer", name="Code Reviewer", icon="scan-search", color="#EAB308",
        description="Code review, best practices, architecture",
        system_prompt="You are a code reviewer. Audit code for bugs, security issues, performance problems, and maintainability. Be thorough but constructive. Suggest specific improvements with code examples.",
    ),
    "security": Role(
        key="security", name="Security Auditor", icon="shield-check", color="#EF4444",
        description="Vulnerability audit, dependency scanning, hardening",
        system_prompt="You are a security specialist. Audit for vulnerabilities: injection, XSS, CSRF, auth bypass, secrets exposure, dependency CVEs. Provide severity ratings and specific fix recommendations.",
    ),
    "architect": Role(
        key="architect", name="Architect", icon="blocks", color="#06B6D4",
        description="System design, planning, technical decisions",
        system_prompt="You are a systems architect. Focus on high-level design, component boundaries, data flow, scalability, and technical tradeoffs. Create clear plans before implementation. Document decisions.",
    ),
    "docs": Role(
        key="docs", name="Documentation", icon="file-text", color="#A855F7",
        description="READMEs, API docs, inline comments, guides",
        system_prompt="You are a documentation specialist. Write clear, concise docs: READMEs, API references, inline comments, architecture guides. Focus on the 'why' not just the 'what'. Include examples.",
    ),
    "general": Role(
        key="general", name="General", icon="bot", color="#64748B",
        description="All-purpose agent, no specialization",
        system_prompt="You are a skilled software engineer. Approach tasks methodically: understand the requirement, explore the codebase, plan your approach, implement, and verify. Ask clarifying questions when needed.",
    ),
}


@dataclass
class OutputSnapshot:
    """Capture of agent output at a key lifecycle point."""
    id: str
    agent_id: str
    trigger: str  # "error", "waiting", "complete", "manual"
    status: str
    summary: str
    line_count: int
    output_tail: str  # Last N lines of output
    context_pct: float
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "trigger": self.trigger,
            "status": self.status,
            "summary": self.summary,
            "line_count": self.line_count,
            "output_tail": self.output_tail,
            "context_pct": self.context_pct,
            "created_at": self.created_at,
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
    plan_mode: bool = False
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
    _stale_warned: bool = field(default=False, repr=False)
    _workflow_stall_warned: bool = field(default=False, repr=False)
    _workflow_hung_warned: bool = field(default=False, repr=False)
    _status_updated_at: float = field(default=0.0, repr=False)  # monotonic time of last status change
    # Intelligence fields — structured output parsing
    _tool_invocations: list = field(default_factory=list, repr=False)  # list[ToolInvocation], capped at 500
    _file_operations: list = field(default_factory=list, repr=False)  # list[FileOperation], capped at 500
    _git_operations: list = field(default_factory=list, repr=False)  # list[GitOperation], capped at 200
    _test_results: list = field(default_factory=list, repr=False)  # list[TestResult], capped at 50
    _snapshots: list = field(default_factory=list, repr=False)  # list[OutputSnapshot], capped at 20
    _status_history: list = field(default_factory=list, repr=False)  # list of {"status": str, "at": float (monotonic)}
    _last_parse_index: int = field(default=0, repr=False)  # Incremental parsing watermark
    _overflow_to_archive: bool = field(default=False, repr=False)
    # Notes and tags
    notes: str = ""
    tags: list = field(default_factory=list)
    # Output bookmarks: list of {"line": int, "text": str, "label": str, "created_at": str}
    bookmarks: list = field(default_factory=list)

    def set_status(self, new_status: str) -> bool:
        """Update status with monotonic timestamp guard. Returns True if updated."""
        now = time.monotonic()
        if now >= self._status_updated_at:
            old_status = self.status
            self.status = new_status
            self._status_updated_at = now
            # Track status transitions for timeline
            if old_status != new_status:
                self._status_history.append({"status": new_status, "at": now})
                if len(self._status_history) > 100:
                    self._status_history = self._status_history[-100:]
            return True
        return False

    def _get_status_timeline(self) -> list[dict]:
        """Build a timeline of status durations for visualization."""
        if not self._status_history:
            return []
        timeline = []
        now = time.monotonic()
        for i, entry in enumerate(self._status_history):
            end_time = self._status_history[i + 1]["at"] if i + 1 < len(self._status_history) else now
            duration_sec = round(end_time - entry["at"], 1)
            if duration_sec > 0:
                timeline.append({"status": entry["status"], "duration_sec": duration_sec})
        return timeline

    def _cost_burn_rate(self) -> dict | None:
        """Calculate cost burn rate ($/min) and estimated time to context exhaustion."""
        if self._spawn_time <= 0 or self.estimated_cost_usd <= 0:
            return None
        uptime_min = (time.monotonic() - self._spawn_time) / 60.0
        if uptime_min < 0.5:
            return None
        rate_per_min = self.estimated_cost_usd / uptime_min
        total_tokens = self.tokens_input + self.tokens_output
        tokens_per_min = total_tokens / uptime_min if uptime_min > 0 else 0
        ctx_window = KNOWN_BACKENDS[self.backend].context_window if self.backend in KNOWN_BACKENDS else 200_000
        remaining_tokens = max(0, ctx_window - self.tokens_input)
        minutes_remaining = remaining_tokens / tokens_per_min if tokens_per_min > 0 else None
        return {
            "cost_per_min": round(rate_per_min, 4),
            "tokens_per_min": round(tokens_per_min),
            "minutes_remaining": round(minutes_remaining, 1) if minutes_remaining else None,
            "uptime_min": round(uptime_min, 1),
        }

    def create_snapshot(self, trigger: str) -> OutputSnapshot:
        """Create a snapshot of current output state."""
        tail_lines = list(self.output_lines)[-50:]
        snap = OutputSnapshot(
            id=uuid.uuid4().hex[:8],
            agent_id=self.id,
            trigger=trigger,
            status=self.status,
            summary=self.summary,
            line_count=self.total_output_lines,
            output_tail="\n".join(tail_lines),
            context_pct=self.context_pct,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._snapshots.append(snap)
        if len(self._snapshots) > 20:
            self._snapshots = self._snapshots[-20:]
        return snap

    def to_dict(self) -> dict:
        role_obj = BUILTIN_ROLES.get(self.role)
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "role_icon": role_obj.icon if role_obj else "bot",
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
            "total_output_chars": self._total_chars,
            "output_rate": round(self.output_rate, 1),
            "files_touched": self.files_touched,
            "model": self.model,
            "tools_allowed": self.tools_allowed,
            "workflow_run_id": self.workflow_run_id,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "cost_burn_rate": self._cost_burn_rate(),
            "plan_mode": self.plan_mode,
            "cost_is_estimated": True,
            "context_window": KNOWN_BACKENDS[self.backend].context_window if self.backend in KNOWN_BACKENDS else 200_000,
            "tool_invocations_count": len(self._tool_invocations),
            "file_operations_count": len(self._file_operations),
            "last_test_result": self._test_results[-1].to_dict() if self._test_results else None,
            "snapshot_count": len(self._snapshots),
            "status_timeline": self._get_status_timeline(),
            "notes": self.notes,
            "tags": list(self.tags),
            "bookmarks": list(self.bookmarks),
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
# Section 3b: Intelligence Data Models
# ─────────────────────────────────────────────


@dataclass
class ToolInvocation:
    """A parsed tool call from agent output."""
    agent_id: str
    tool: str  # Read, Edit, Write, Bash, Glob, Grep, etc.
    args: str  # Primary argument (file path, command, pattern)
    timestamp: float  # monotonic time
    line_index: int  # Output line index where this was detected
    result_status: str = "unknown"  # success, error, unknown
    result_snippet: str = ""  # First ~100 chars of result

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "tool": self.tool,
            "args": self.args,
            "timestamp": self.timestamp,
            "result_status": self.result_status,
            "result_snippet": self.result_snippet,
        }


@dataclass
class FileOperation:
    """A file read/write/create detected from agent output."""
    agent_id: str
    file_path: str
    operation: str  # read, write, create, edit, delete
    timestamp: float
    tool: str = ""  # Which tool performed it

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "file_path": self.file_path,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "tool": self.tool,
        }


@dataclass
class GitOperation:
    """A git operation detected from agent output."""
    agent_id: str
    operation: str  # commit, checkout, branch, merge, push, pull
    detail: str  # commit message, branch name, etc.
    timestamp: float
    files_affected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "operation": self.operation,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "files_affected": self.files_affected,
        }


@dataclass
class TestResult:
    """Parsed test run results from agent output."""
    agent_id: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    coverage_pct: float | None = None
    framework: str = ""  # pytest, jest, mocha, etc.
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "coverage_pct": self.coverage_pct,
            "framework": self.framework,
            "timestamp": self.timestamp,
        }


@dataclass
class AgentInsight:
    """A cross-agent insight or alert from the meta-agent."""
    id: str
    insight_type: str  # conflict, stuck, handoff, anomaly, suggestion
    severity: str  # info, warning, critical
    message: str
    agent_ids: list[str] = field(default_factory=list)
    evidence: str = ""
    suggested_action: str = ""
    acknowledged: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "insight_type": self.insight_type,
            "severity": self.severity,
            "message": self.message,
            "agent_ids": self.agent_ids,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "acknowledged": self.acknowledged,
            "created_at": self.created_at,
        }


@dataclass
class QueuedTask:
    """A task waiting to be auto-spawned when agent slots are available."""
    id: str
    role: str
    name: str
    task: str
    working_dir: str = ""
    backend: str = ""
    plan_mode: bool = False
    project_id: str | None = None
    priority: int = 0  # Higher = spawned first
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "task": self.task,
            "working_dir": self.working_dir,
            "backend": self.backend,
            "plan_mode": self.plan_mode,
            "project_id": self.project_id,
            "priority": self.priority,
            "created_at": self.created_at,
        }


@dataclass
class ParsedIntent:
    """Result of NLU command parsing."""
    action: str  # spawn, kill, pause, resume, send, status, query
    targets: list[str] = field(default_factory=list)  # Agent IDs or names
    filter: str = ""  # Role, status, or project filter
    message: str = ""  # Message to send
    parameters: dict = field(default_factory=dict)  # Extra params
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "targets": self.targets,
            "filter": self.filter,
            "message": self.message,
            "parameters": self.parameters,
            "confidence": self.confidence,
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
        # Task queue for auto-spawning
        self.task_queue: list[QueuedTask] = []
        # Server stats
        self._start_time: float = time.monotonic()
        self._total_spawned: int = 0
        self._total_killed: int = 0
        self._total_messages_sent: int = 0

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
                supports_prompt_arg=bc.supports_prompt_arg,
                auto_approve_flag=bc.auto_approve_flag,
                plan_mode_flag=bc.plan_mode_flag,
                status_patterns=bc.status_patterns,
                inject_role_prompt=bc.inject_role_prompt,
                cost_input_per_1k=bc.cost_input_per_1k,
                cost_output_per_1k=bc.cost_output_per_1k,
                context_window=bc.context_window,
                char_to_token_ratio=bc.char_to_token_ratio,
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

    async def _wait_for_tui_ready(self, session: str, timeout: float = 15.0) -> bool:
        """Poll tmux output until we detect a CLI TUI is ready for input.

        Looks for the CLI status bar (bypass permissions) as the primary indicator
        that the TUI is fully loaded and ready to accept Enter. Returns True if
        ready, False on timeout.
        """
        # Only look for strong indicators that the TUI is fully initialized.
        # "bypass permissions" appears in Claude Code's status bar after full load.
        # Avoid ❯ or > as they can appear during partial renders.
        ready_patterns = ["bypass permissions", "Enter a prompt", "shift+tab to cycle"]
        start = asyncio.get_event_loop().time()
        poll_count = 0
        while (asyncio.get_event_loop().time() - start) < timeout:
            await asyncio.sleep(1.5)
            poll_count += 1
            lines = await self._tmux_capture(session, lines=50)
            if lines is None:
                continue  # session not ready yet
            # Search ALL captured lines — TUI status bar can be anywhere in the pane
            full_text = "\n".join(lines) if lines else ""
            for pattern in ready_patterns:
                if pattern in full_text:
                    elapsed = asyncio.get_event_loop().time() - start
                    log.info(f"TUI ready for {session}: found '{pattern}' after {elapsed:.1f}s ({poll_count} polls)")
                    return True
        log.warning(f"TUI ready timeout ({timeout}s) for {session} after {poll_count} polls")
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

    async def _tmux_capture(self, session: str, lines: int = 200) -> list[str] | None:
        """Capture terminal output. Returns lines on success, empty list if no output, None if tmux error."""
        try:
            result = await self._run_tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
            if result.returncode == 0:
                return result.stdout.splitlines()
            log.debug(f"tmux capture failed for {session}: exit={result.returncode} stderr={result.stderr[:200]}")
            return None  # Signal capture failure (session may be dead)
        except Exception as e:
            log.debug(f"tmux capture exception for {session}: {e}")
            return None

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
            plan_mode=plan_mode,
            _spawn_time=time.monotonic(),
        )
        agent.last_output_time = time.monotonic()
        self.agents[agent_id] = agent
        self._total_spawned += 1

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
                try:
                    await self._run_tmux(["kill-session", "-t", session_name])
                except Exception:
                    pass
                return agent
        except Exception as e:
            agent.status = "error"
            agent.error_message = str(e)
            try:
                await self._run_tmux(["kill-session", "-t", session_name])
            except Exception:
                pass
            return agent

        # Get pane PID
        agent.pid = await self._tmux_get_pane_pid(session_name)

        if self.config.demo_mode:
            # Demo mode: run a bash script that simulates agent behavior
            try:
                demo_script = self._build_demo_script(role, task, agent)
                await self._tmux_send_keys(session_name, demo_script)
            except Exception as e:
                agent.status = "error"
                agent.error_message = f"Demo script setup failed: {e}"
                log.error(f"Demo script setup failed for {agent_id}: {e}")
                # Clean up temp script if it was created
                if agent.script_path:
                    try:
                        Path(agent.script_path).unlink(missing_ok=True)
                        agent.script_path = None
                    except Exception:
                        pass
                # Kill orphaned tmux session
                try:
                    await self._run_tmux(["kill-session", "-t", session_name])
                except Exception:
                    pass
                return agent
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
                    # Kill orphaned tmux session
                    try:
                        await self._run_tmux(["kill-session", "-t", session_name])
                    except Exception:
                        pass
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

            # Plan mode: use backend-specific plan flag (incompatible with auto-approve)
            if plan_mode and bc.plan_mode_flag:
                if bc.auto_approve_flag and bc.auto_approve_flag in cmd_parts:
                    cmd_parts.remove(bc.auto_approve_flag)
                cmd_parts.extend(bc.plan_mode_flag.split())

            # System prompt injection (role context + predecessor output)
            # Skip role prompt for backends that manage their own context (e.g. claude-code has CLAUDE.md)
            role_obj = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"])
            parts = []
            if bc.inject_role_prompt and role != "general":
                parts.append(f"You are a {role_obj.name}. {role_obj.system_prompt}")
            if system_prompt_extra:
                parts.append(system_prompt_extra)
            full_system = "\n\n".join(parts)
            if full_system and bc.supports_system_prompt:
                cmd_parts.extend(["--append-system-prompt", full_system])
                agent.system_prompt = full_system

            # Session resume
            if resume_session and bc.supports_session_resume:
                cmd_parts.extend(["--resume", resume_session])
                agent.session_id = resume_session

            # Prepare task text — plan-mode prefix for backends without native plan flag
            if plan_mode and not bc.plan_mode_flag:
                task_to_send = (
                    "IMPORTANT: Create a detailed plan for the following task. "
                    "DO NOT execute any changes. Present your plan and ask for approval.\n\n"
                    f"Task: {task}"
                )
            else:
                task_to_send = task

            # Include task as CLI positional arg if supported (e.g. claude-code)
            # Must be the last argument (positional, not a flag)
            if bc.supports_prompt_arg and task_to_send:
                cmd_parts.append(task_to_send)

            # Build command string and launch
            cmd = " ".join(shlex.quote(p) for p in cmd_parts)
            await self._tmux_send_keys(session_name, cmd)

            if bc.supports_prompt_arg:
                # CLI pre-fills the prompt — poll until TUI is ready, then send Enter
                ready = await self._wait_for_tui_ready(session_name)
                if not ready:
                    log.warning(f"Agent {agent_id}: TUI not ready after timeout, sending Enter anyway")
                # Settle delay — TUI needs a moment after rendering before accepting input
                await asyncio.sleep(2.0)
                await self._tmux_send_raw(session_name, "Enter")

            # If task was NOT included as CLI arg, send it after startup
            if not bc.supports_prompt_arg:
                await asyncio.sleep(3)  # Wait for CLI to start up
                if bc.supports_system_prompt:
                    # System prompt already injected via CLI flag — just send task
                    if task_to_send:
                        await self._tmux_send_keys(session_name, task_to_send)
                else:
                    # Fallback: send role context + task as first message
                    if role_obj and role_obj.system_prompt and task_to_send:
                        full_message = f"{role_obj.system_prompt}\n\nYour task: {task_to_send}"
                        for line in full_message.split("\n"):
                            if line.strip():
                                await self._tmux_send_keys(session_name, line)
                                await asyncio.sleep(0.1)
                    elif task_to_send:
                        await self._tmux_send_keys(session_name, task_to_send)

        agent.status = "planning" if plan_mode else "working"
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        log.info(f"Spawned agent {agent_id} ({name}) with role {role}")
        return agent

    def _build_demo_script(self, role: str, task: str, agent: Agent | None = None) -> str:
        """Build a multi-phase bash script that simulates realistic agent behavior."""
        import random as _rand
        role_obj = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"])
        safe_task = task[:80].replace("'", "'\\''")  # escape for single-quoted bash strings

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
            return f"sleep {_rand.uniform(lo, hi):.1f}"

        # Build script lines — header is shared
        script_lines = [
            '#!/bin/bash',
            f'echo "╭──────────────────────────────────────────╮"',
            f'echo "│ [{role_obj.icon}] Ashlar Agent (Demo Mode)          │"',
            f'echo "│ Role: {role_obj.name:<34}│"',
            f'echo "╰──────────────────────────────────────────╯"',
            f'echo ""',
            f"echo 'Task: {safe_task}'",
            f'echo ""',
        ]

        is_plan_mode = agent.plan_mode if agent else False

        if is_plan_mode:
            # Plan mode: analyze → present plan → wait for approval → then work
            script_lines.extend([
                'echo "Planning approach..."',
                rsleep(1.5, 3.0),
                'echo "Let me analyze the codebase structure first."',
                rsleep(1.0, 2.0),
                'echo "Reading project files..."',
                rsleep(1.5, 2.5),
                'echo "Analyzing dependencies and patterns..."',
                rsleep(2.0, 3.0),
                'echo ""',
                'echo "╭──────────────────────────────────────────╮"',
                'echo "│  PLAN                                    │"',
                'echo "╰──────────────────────────────────────────╯"',
                'echo ""',
                'echo "Here is my proposed plan:"',
                'echo ""',
                'echo "  1. Read existing code and understand patterns"',
                'echo "     - Scan src/ for relevant files"',
                'echo "     - Identify existing abstractions to reuse"',
                'echo ""',
                'echo "  2. Implement the required changes"',
                'echo "     - Create/modify 3-4 files"',
                'echo "     - Follow existing code conventions"',
                'echo ""',
                'echo "  3. Write tests and verify correctness"',
                'echo "     - Add unit tests for new logic"',
                'echo "     - Run existing test suite for regressions"',
                'echo ""',
                'echo "  4. Clean up and document"',
                'echo "     - Remove dead code"',
                'echo "     - Add inline comments where needed"',
                'echo ""',
                rsleep(1.0, 2.0),
                'echo "Do you want me to proceed with this plan? (yes/no)"',
                'read -r REPLY',
                'echo ""',
                'echo "Received: $REPLY"',
                rsleep(1.0, 2.0),
                'echo "Plan approved. Starting implementation..."',
                'echo ""',
            ])
            # After approval, run work phase
            for msg in work_msgs:
                script_lines.append(f'echo "{msg}"')
                script_lines.append(rsleep(1.0, 4.0))
            script_lines.extend([
                'echo ""',
                'echo "Done! Task completed successfully."',
                'sleep 86400',
            ])
        else:
            # Normal mode: plan briefly → work → question → more work → done
            script_lines.extend([
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
            ])

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
        except Exception as e:
            log.warning(f"Failed to send /exit to tmux session {session} for agent {agent_id}: {e}")

        # Force kill tmux session
        try:
            await self._run_tmux(["kill-session", "-t", session])
        except Exception as e:
            log.warning(f"Failed to kill tmux session {session} for agent {agent_id}: {e}")

        # Clean up demo script temp file
        if agent.script_path:
            try:
                Path(agent.script_path).unlink(missing_ok=True)
            except Exception as e:
                log.warning(f"Failed to remove demo script {agent.script_path} for agent {agent_id}: {e}")

        self._cleanup_file_activity(agent_id)
        del self.agents[agent_id]
        self._total_killed += 1
        return True

    async def pause(self, agent_id: str) -> bool:
        """Pause agent by sending Ctrl+C."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        await self._tmux_send_raw(agent.tmux_session, "C-c")
        agent.set_status("paused")
        agent.needs_input = False
        agent.input_prompt = None
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
        agent.set_status("working")
        agent.needs_input = False
        agent.input_prompt = None
        log.info(f"Resumed agent {agent_id}")
        return True

    async def restart(self, agent_id: str, new_task: str | None = None) -> bool:
        """Restart an agent by killing its tmux session and re-spawning with same config.
        If new_task is provided, overrides the agent's task for the restart.
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
            saved_plan_mode = agent.plan_mode

            # Apply task override if provided
            if new_task:
                saved_task = new_task
                agent.task = new_task

            # Kill the old tmux session
            old_tmux_session = agent.tmux_session
            try:
                await self._tmux_send_keys(old_tmux_session, "/exit")
                await asyncio.sleep(1)
            except Exception as e:
                log.warning(f"Restart: failed to send /exit to old session {old_tmux_session} for agent {agent_id}: {e}")
            try:
                await self._run_tmux(["kill-session", "-t", old_tmux_session])
            except Exception as e:
                log.warning(f"Restart: failed to kill old tmux session {old_tmux_session} for agent {agent_id}: {e}")

            # Verify old session is gone
            try:
                check = await self._run_tmux(["has-session", "-t", old_tmux_session])
                if check.returncode == 0:
                    await self._run_tmux(["kill-session", "-t", old_tmux_session])
            except Exception as e:
                log.warning(f"Restart: failed to verify/force-kill old session {old_tmux_session} for agent {agent_id}: {e}")

            # Clean up demo script temp file
            if agent.script_path:
                try:
                    Path(agent.script_path).unlink(missing_ok=True)
                except Exception as e:
                    log.warning(f"Restart: failed to remove demo script {agent.script_path} for agent {agent_id}: {e}")

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
            agent._archived_lines = 0
            agent._overflow_to_archive = None
            agent.context_pct = 0.0
            agent.tokens_input = 0
            agent.tokens_output = 0
            agent.estimated_cost_usd = 0.0
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

                # Plan mode: use backend-specific plan flag (incompatible with auto-approve)
                if saved_plan_mode and bc.plan_mode_flag:
                    if bc.auto_approve_flag and bc.auto_approve_flag in cmd_parts:
                        cmd_parts.remove(bc.auto_approve_flag)
                    cmd_parts.extend(bc.plan_mode_flag.split())

                # Rebuild system prompt from role + saved extra
                role_obj = BUILTIN_ROLES.get(saved_role, BUILTIN_ROLES["general"])
                if bc.supports_system_prompt and saved_system_prompt:
                    cmd_parts.extend(["--append-system-prompt", saved_system_prompt])

                # Prepare task text — plan-mode prefix for backends without native plan flag
                if saved_plan_mode and not bc.plan_mode_flag:
                    task_to_send = (
                        "IMPORTANT: Create a detailed plan for the following task. "
                        "DO NOT execute any changes. Present your plan and ask for approval.\n\n"
                        f"Task: {saved_task}"
                    )
                else:
                    task_to_send = saved_task

                # Include task as CLI positional arg if supported
                if bc.supports_prompt_arg and task_to_send:
                    cmd_parts.append(task_to_send)

                cmd = " ".join(shlex.quote(p) for p in cmd_parts)
                await self._tmux_send_keys(session_name, cmd)

                if bc.supports_prompt_arg:
                    # Poll until TUI is ready, then send Enter
                    ready = await self._wait_for_tui_ready(session_name)
                    if not ready:
                        log.warning(f"Agent {agent_id}: TUI not ready after timeout on restart, sending Enter anyway")
                    await asyncio.sleep(2.0)
                    await self._tmux_send_raw(session_name, "Enter")

                # If task was NOT included as CLI arg, send it after startup
                if not bc.supports_prompt_arg:
                    await asyncio.sleep(3)
                    if bc.supports_system_prompt:
                        if task_to_send:
                            await self._tmux_send_keys(session_name, task_to_send)
                    else:
                        if role_obj and role_obj.system_prompt and task_to_send:
                            full_message = f"{role_obj.system_prompt}\n\nYour task: {task_to_send}"
                            for line in full_message.split("\n"):
                                if line.strip():
                                    await self._tmux_send_keys(session_name, line)
                                    await asyncio.sleep(0.1)
                        elif task_to_send:
                            await self._tmux_send_keys(session_name, task_to_send)

            agent.plan_mode = saved_plan_mode
            agent.status = "planning" if saved_plan_mode else "working"
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
        self._total_messages_sent += 1
        return True

    async def capture_output(self, agent_id: str) -> list[str] | None:
        """Capture terminal output and return new lines since last capture.
        Returns None if tmux session is dead/unreachable (vs empty list for no new output)."""
        agent = self.agents.get(agent_id)
        if not agent:
            return None

        raw_lines = await self._tmux_capture(agent.tmux_session)
        if raw_lines is None:
            return None  # Propagate tmux failure signal
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
            current_offset = agent._archived_lines
            agent._overflow_to_archive = (agent_id, overflow_lines, current_offset)
            agent._archived_lines += len(overflow_lines)  # Pre-increment; offset captured above

        # Update ring buffer (only new lines, not the full capture)
        for line in new_lines:
            agent.output_lines.append(line)

        # Track total chars for context estimation and cost tracking
        chars_added = sum(len(l) for l in new_lines)
        agent._total_chars += chars_added
        # Use backend-specific context window and char/token ratio
        bc = self.backend_configs.get(agent.backend)
        ctx_window = bc.context_window if bc else 200_000
        char_ratio = bc.char_to_token_ratio if bc else 3.5
        # Base context includes: system prompt (~8K tokens for Claude Code),
        # tool definitions (~12K tokens), task description, and per-turn overhead.
        # Claude Code's context includes both input AND output in the conversation window.
        system_overhead_tokens = 20_000 if agent.backend == "claude-code" else 5_000
        task_tokens = len(agent.task or '') / char_ratio
        prompt_tokens = len(agent.system_prompt or '') / char_ratio
        # Output chars become BOTH output tokens AND input tokens in the next turn
        # (conversation history is re-sent each turn), so effective multiplier is ~1.8x
        output_tokens = agent._total_chars / char_ratio
        effective_tokens = system_overhead_tokens + task_tokens + prompt_tokens + (output_tokens * 1.8)
        agent.context_pct = min(1.0, effective_tokens / ctx_window)

        # Override with parsed context indicators from output (more accurate)
        parsed_ctx = self._detect_context_from_output(new_lines, agent.backend)
        if parsed_ctx is not None:
            agent.context_pct = parsed_ctx

        # Token/cost estimation (approximate — marked as estimates in API)
        # Use same char_ratio for consistency with context calculation
        tokens_est = int(chars_added / char_ratio)
        agent.tokens_output += tokens_est
        # Input tokens: base context + conversation history (output re-read on each turn)
        agent.tokens_input = int(system_overhead_tokens + task_tokens + prompt_tokens) + int(agent.tokens_output * 0.8)
        # Compute cost from backend config rates
        if bc:
            agent.estimated_cost_usd = (
                (agent.tokens_input / 1000) * bc.cost_input_per_1k +
                (agent.tokens_output / 1000) * bc.cost_output_per_1k
            )

        # Cost budget check
        budget = self.config.cost_budget_usd
        if budget > 0 and agent.estimated_cost_usd >= budget:
            if not getattr(agent, '_budget_warned', False):
                agent._budget_warned = True
                log.warning("Agent %s exceeded cost budget ($%.2f / $%.2f)",
                            agent.name, agent.estimated_cost_usd, budget)

        return new_lines

    # ── Context Patterns ──
    _CONTEXT_PATTERNS = [
        re.compile(r"context.*?(\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
        re.compile(r"(\d+(?:\.\d+)?)%\s*(?:of\s+)?context", re.IGNORECASE),
        re.compile(r"(\d+)[Kk]\s*(?:of\s+)?(\d+)[Kk]\s*tokens"),
        re.compile(r"compacting\s+conversation", re.IGNORECASE),
        re.compile(r"context\s+window.*?(\d+)", re.IGNORECASE),
    ]

    def _detect_context_from_output(self, lines: list[str], backend: str) -> float | None:
        """Parse output lines for context window indicators.
        Returns a float 0.0-1.0 if detected, None if no indicator found."""
        if backend != "claude-code":
            return None
        for line in lines:
            # Check for compaction first (near-full context)
            if re.search(r"compacting\s+conversation", line, re.IGNORECASE):
                return 0.95
            # Percentage pattern: "context 73%" or "73% context"
            m = re.search(r"context.*?(\d+(?:\.\d+)?)\s*%", line, re.IGNORECASE)
            if m:
                return min(1.0, float(m.group(1)) / 100.0)
            m = re.search(r"(\d+(?:\.\d+)?)%\s*(?:of\s+)?context", line, re.IGNORECASE)
            if m:
                return min(1.0, float(m.group(1)) / 100.0)
            # Token ratio: "142K of 200K tokens" or "142K/200K"
            m = re.search(r"(\d+)[Kk]\s*(?:of|/)\s*(\d+)[Kk]", line)
            if m:
                used = float(m.group(1))
                total = float(m.group(2))
                if total > 0:
                    return min(1.0, used / total)
        return None

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
        detected = parse_agent_status(recent, agent, bp)

        # Plan-mode guard: keep agent in "planning" until it produces a plan (waiting).
        # Agent flow: planning → waiting (plan ready) → working (after approval)
        # Block transitions to any work-like status; allow waiting/error to pass through.
        if agent.plan_mode and agent.status == "planning" and detected not in ("waiting", "error", "planning"):
            return "planning"

        return detected

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
        for agent in list(self.agents.values()):
            if agent.script_path:
                try:
                    Path(agent.script_path).unlink(missing_ok=True)
                except Exception as e:
                    log.warning(f"Cleanup: failed to remove demo script {agent.script_path}: {e}")

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
                        except Exception as e:
                            log.warning(f"Cleanup: failed to kill tmux session {session_name}: {e}")
        except Exception as e:
            log.warning(f"Cleanup: failed to list/kill tmux sessions: {e}")
        log.info("All agent sessions cleaned up")

    def cleanup_orphaned_sessions(self) -> int:
        """Kill any ashlar-* tmux sessions left from previous ungraceful shutdowns. Returns count killed."""
        killed = 0
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
                            killed += 1
                        except Exception:
                            pass
        except Exception:
            pass
        return killed


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
        re.compile(r"<thinking>"),  # Claude thinking blocks
    ],
    "reading": [
        re.compile(r"(?i)(reading|loading|scanning|parsing) .+\.\w+"),
        re.compile(r"(?i)(reading|loading|scanning|parsing) (directory|folder|project|codebase)"),
        re.compile(r"(?i)exploring .+"),
        re.compile(r"(?i)searching (for|in|through|across)"),
        re.compile(r"(?i)(looking at|examining|inspecting|reviewing) .+"),
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
        # Claude Code tool calls
        re.compile(r"(?i)(Read|Write|Edit|Glob|Grep|Bash)\("),
        re.compile(r"(?i)Tool (Result|Output):"),
        # Package management / network
        re.compile(r"(?i)(installing|downloading|fetching|uploading)"),
        # Linting / formatting (active operations, not result messages)
        re.compile(r"(?i)(linting|formatting|running (lint|prettier|eslint|mypy|ruff))"),
        # Docker / container operations
        re.compile(r"(?i)(docker (build|run|pull|push|compose))"),
        # Database operations
        re.compile(r"(?i)(migrating|seeding|running migration)"),
        # Deploy operations
        re.compile(r"(?i)(deploying|pushing to|releasing)"),
    ],
    "waiting": [
        re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
        re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
        re.compile(r"(?i)proceed\?"),
        re.compile(r"(?i)\bapprove\b"),
        re.compile(r"(?i)please (confirm|respond|reply|answer|choose|select)"),
        re.compile(r"(?i)waiting for (input|response|confirmation|approval)"),
        re.compile(r"(?i)enter (a |your )?(value|name|path|password|token)"),
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
        re.compile(r"(?i)changes committed"),  # Git commit completion
        re.compile(r"(?i)no (issues|errors|warnings) found"),  # Clean lint/test
    ],
}

WAITING_LINE_PATTERNS = [
    re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
    re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
    re.compile(r"(?i)proceed\?"),
    # Only match trailing ? if the line looks like a direct question to the user
    # (contains "you", "I", or starts with a question word)
    re.compile(r"(?i)^(do|can|will|should|shall|would|may|is|are|does|did|have|has)\b.+\?\s*$"),
]

# Follow-up suggestions based on role completion
FOLLOWUP_SUGGESTIONS: dict[str, list[dict]] = {
    "backend": [
        {"role": "tester", "task_template": "Write tests for the changes made by {name}: {summary}",
         "reason": "Backend changes should be tested"},
        {"role": "reviewer", "task_template": "Review the code changes made by {name}: {summary}",
         "reason": "Backend code should be reviewed"},
    ],
    "frontend": [
        {"role": "tester", "task_template": "Test the UI changes made by {name}: {summary}",
         "reason": "Frontend changes need testing"},
        {"role": "reviewer", "task_template": "Review the frontend changes by {name}: {summary}",
         "reason": "Frontend code should be reviewed"},
    ],
    "architect": [
        {"role": "backend", "task_template": "Implement the architecture designed by {name}: {summary}",
         "reason": "Architecture plans need implementation"},
    ],
    "security": [
        {"role": "backend", "task_template": "Fix the security issues found by {name}: {summary}",
         "reason": "Security findings need remediation"},
    ],
    "reviewer": [
        {"role": "backend", "task_template": "Address the review feedback from {name}: {summary}",
         "reason": "Review feedback should be addressed"},
    ],
    "tester": [
        {"role": "backend", "task_template": "Fix the test failures found by {name}: {summary}",
         "reason": "Failed tests need fixes"},
    ],
}

def _suggest_followup(agent: Agent) -> dict | None:
    """Suggest a follow-up agent based on what this agent did."""
    suggestions = FOLLOWUP_SUGGESTIONS.get(agent.role, [])
    if not suggestions:
        return None
    # Pick the first applicable suggestion
    s = suggestions[0]
    task = s["task_template"].format(name=agent.name, summary=(agent.summary or agent.task or "")[:100])
    return {
        "suggested_role": s["role"],
        "suggested_task": task,
        "reason": s["reason"],
        "source_agent_id": agent.id,
        "source_agent_name": agent.name,
        "message": f'{agent.name} completed — {s["reason"]}. Suggested: spawn {s["role"]} agent.',
    }


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

    # Check for waiting (highest priority, only in recent lines to avoid stale matches)
    for pattern in effective_patterns["waiting"]:
        if pattern.search(tail_text):
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
        m = pattern.search(tail_text)
        if m:
            agent.needs_input = False
            # Capture the matching line as error context
            for line in reversed(recent_lines[-5:]):
                if pattern.search(line):
                    agent.error_message = _strip_ansi(line.strip())[:200]
                    break
            return "error"

    # Check for reading (before working, more specific)
    for pattern in effective_patterns["reading"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "reading"

    # Check for planning (only in recent lines to avoid matching task descriptions)
    for pattern in effective_patterns["planning"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "planning"

    # Check for working (use tail_text to avoid matching stale output)
    # Must come before "complete" so active-work indicators win over prompt presence
    for pattern in effective_patterns["working"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "working"

    # Check for complete / idle (e.g. CLI prompt visible, no active work)
    for pattern in effective_patterns["complete"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "idle"

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
    re.compile(r"(?i)(committed .+ to .+)"),  # Git commit
]

# Claude Code intent patterns — "I'll fix the auth bug" → extract intent
_INTENT_RE = re.compile(r"(?i)I'll\s+(.{10,80})")
_GIT_COMMIT_RE = re.compile(r"(?i)committed .+ to .+")


def extract_summary(lines: list[str], task: str, status: str = "") -> str:
    """Extract a 1-2 line summary from recent output with file paths and test results."""
    MAX_LEN = 80  # Tighter cap for card display

    # Check for test results
    for line in reversed(lines[-20:]):
        m = _TEST_RESULT_RE.search(line)
        if m:
            return _strip_ansi(f"Tests: {m.group(1)} passed, {m.group(2)} failed")[:MAX_LEN]
        m = _COVERAGE_RE.search(line)
        if m:
            return _strip_ansi(f"Coverage: {m.group(1)}%")[:MAX_LEN]

    # Git commit summaries
    for line in reversed(lines[-20:]):
        m = _GIT_COMMIT_RE.search(_strip_ansi(line.strip()))
        if m:
            return _strip_ansi(m.group(0))[:MAX_LEN]

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
                    return f"{match.group(1).title()} {fp.group(0)}"[:MAX_LEN]
                return stripped[:MAX_LEN]

    # Claude Code intent patterns — "I'll fix the authentication bug"
    for line in reversed(lines[-20:]):
        m = _INTENT_RE.search(_strip_ansi(line.strip()))
        if m:
            return m.group(1).strip().rstrip(".")[:MAX_LEN]

    # Files progress
    for line in reversed(lines[-15:]):
        m = _FILES_PROGRESS_RE.search(_strip_ansi(line))
        if m:
            return f"Progress: {m.group(1)} of {m.group(2)} files"[:MAX_LEN]

    # When status is error, show first error line
    if status == "error":
        for line in reversed(lines[-20:]):
            stripped = _strip_ansi(line.strip())
            if stripped and re.search(r"(?i)(error|traceback|fatal|panic)", stripped):
                return stripped[:MAX_LEN]

    # Fallback: last non-empty line (skip TUI chrome from Claude Code etc.)
    _TUI_CHROME = {
        "bypass permissions", "shift+tab to cycle", "Welcome back",
        "Recent activity", "/resume for more", "What's new",
        "/release-notes", "Enter a prompt", "Claude Code v",
    }
    for line in reversed(lines[-10:]):
        stripped = _strip_ansi(line.strip())
        if not stripped or len(stripped) <= 5:
            continue
        # Skip box-drawing lines and TUI chrome
        if stripped[0] in "╭╰│╔╗╚╝║─━┌┐└┘├┤┬┴┼▐▝❯":
            continue
        if any(chrome in stripped for chrome in _TUI_CHROME):
            continue
        return stripped[:MAX_LEN]

    return _strip_ansi(task[:MAX_LEN]) if task else "Working..."


# ── Structured Output Intelligence Parser ──

class OutputIntelligenceParser:
    """Pure regex parser for structured output from Claude Code and other agent backends.
    Runs in the capture loop — ~0.1ms per agent, non-blocking.
    """

    # Tool invocation patterns (Claude Code format)
    _TOOL_PATTERNS = [
        (re.compile(r'Read\("([^"]+)"\)'), "Read"),
        (re.compile(r'Edit\("([^"]+)"\)'), "Edit"),
        (re.compile(r'Write\("([^"]+)"\)'), "Write"),
        (re.compile(r'Bash\("([^"]{0,200})'), "Bash"),
        (re.compile(r'Glob\("([^"]+)"\)'), "Glob"),
        (re.compile(r'Grep\("([^"]+)"\)'), "Grep"),
        (re.compile(r'LS\("([^"]+)"\)'), "LS"),
        (re.compile(r'Task\("([^"]{0,100})'), "Task"),
        (re.compile(r'WebFetch\("([^"]+)"\)'), "WebFetch"),
        (re.compile(r'WebSearch\("([^"]+)"\)'), "WebSearch"),
        (re.compile(r'NotebookEdit\("([^"]+)"\)'), "NotebookEdit"),
    ]

    # Tool result patterns
    _RESULT_SUCCESS = re.compile(r'(?:Tool Result|Result|✓|✔|Success|Updated|Created|Written)')
    _RESULT_ERROR = re.compile(r'(?:Error|Failed|✕|✗|Exception|FAILED)')

    # Git operation patterns
    _GIT_COMMIT = re.compile(r'git commit.*-m\s+["\'](.{0,120})')
    _GIT_CHECKOUT = re.compile(r'git checkout\s+(\S+)')
    _GIT_BRANCH = re.compile(r'git (?:branch|switch)\s+(\S+)')
    _GIT_PUSH = re.compile(r'git push\s+(\S+)')
    _GIT_MERGE = re.compile(r'git merge\s+(\S+)')
    _COMMIT_SHA = re.compile(r'\b([0-9a-f]{7,40})\b.*(?:commit|created|pushed)')

    # Test result patterns
    _PYTEST = re.compile(r'(\d+)\s+passed(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+skipped)?')
    _JEST = re.compile(r'Tests:\s+(?:(\d+)\s+passed)?(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+total)?')
    _MOCHA = re.compile(r'(\d+)\s+passing.*?(?:(\d+)\s+failing)?')
    _COVERAGE = re.compile(r'(?:coverage|Coverage|TOTAL).*?(\d{1,3}(?:\.\d+)?)%')

    # File operation patterns (from natural language output)
    _FILE_READ = re.compile(r'(?:Reading|Scanning|Analyzing)\s+[`"]?([^\s`"]+\.\w{1,8})[`"]?')
    _FILE_WRITE = re.compile(r'(?:Writing|Creating|Updating|Editing)\s+[`"]?([^\s`"]+\.\w{1,8})[`"]?')

    def parse_incremental(self, agent: "Agent") -> dict:
        """Parse new output lines since last call. Returns counts of new items parsed."""
        lines = list(agent.output_lines)
        start_idx = agent._last_parse_index
        if start_idx >= len(lines):
            return {"tools": 0, "files": 0, "git": 0, "tests": 0}

        new_lines = lines[start_idx:]
        agent._last_parse_index = len(lines)
        now = time.monotonic()

        counts = {"tools": 0, "files": 0, "git": 0, "tests": 0}

        for i, line in enumerate(new_lines):
            line_idx = start_idx + i
            stripped = _strip_ansi(line) if callable(_strip_ansi) else line

            # Tool invocations
            for pattern, tool_name in self._TOOL_PATTERNS:
                m = pattern.search(stripped)
                if m:
                    inv = ToolInvocation(
                        agent_id=agent.id,
                        tool=tool_name,
                        args=m.group(1),
                        timestamp=now,
                        line_index=line_idx,
                    )
                    agent._tool_invocations.append(inv)
                    if len(agent._tool_invocations) > 500:
                        agent._tool_invocations = agent._tool_invocations[-500:]
                    counts["tools"] += 1

                    # Also record file operations for Read/Edit/Write
                    if tool_name in ("Read", "Edit", "Write", "Glob", "LS"):
                        op_type = "read" if tool_name in ("Read", "Glob", "LS") else "edit" if tool_name == "Edit" else "write"
                        fop = FileOperation(
                            agent_id=agent.id,
                            file_path=m.group(1),
                            operation=op_type,
                            timestamp=now,
                            tool=tool_name,
                        )
                        agent._file_operations.append(fop)
                        if len(agent._file_operations) > 500:
                            agent._file_operations = agent._file_operations[-500:]
                        counts["files"] += 1
                    break  # One tool per line

            # Tool results — update last invocation's result_status
            if agent._tool_invocations:
                if self._RESULT_SUCCESS.search(stripped):
                    agent._tool_invocations[-1].result_status = "success"
                    agent._tool_invocations[-1].result_snippet = stripped[:100]
                elif self._RESULT_ERROR.search(stripped):
                    agent._tool_invocations[-1].result_status = "error"
                    agent._tool_invocations[-1].result_snippet = stripped[:100]

            # Git operations
            for git_re, git_op in [
                (self._GIT_COMMIT, "commit"),
                (self._GIT_CHECKOUT, "checkout"),
                (self._GIT_BRANCH, "branch"),
                (self._GIT_PUSH, "push"),
                (self._GIT_MERGE, "merge"),
            ]:
                m = git_re.search(stripped)
                if m:
                    gop = GitOperation(
                        agent_id=agent.id,
                        operation=git_op,
                        detail=m.group(1),
                        timestamp=now,
                    )
                    agent._git_operations.append(gop)
                    if len(agent._git_operations) > 200:
                        agent._git_operations = agent._git_operations[-200:]
                    counts["git"] += 1
                    break

            # Test results
            for test_re, framework in [
                (self._PYTEST, "pytest"),
                (self._JEST, "jest"),
                (self._MOCHA, "mocha"),
            ]:
                m = test_re.search(stripped)
                if m:
                    groups = m.groups()
                    passed = int(groups[0] or 0)
                    failed = int(groups[1] or 0) if len(groups) > 1 else 0
                    skipped = int(groups[2] or 0) if len(groups) > 2 else 0
                    tr = TestResult(
                        agent_id=agent.id,
                        passed=passed,
                        failed=failed,
                        skipped=skipped,
                        total=passed + failed + skipped,
                        framework=framework,
                        timestamp=now,
                    )
                    # Check for coverage on same or nearby lines
                    cov_m = self._COVERAGE.search(stripped)
                    if cov_m:
                        tr.coverage_pct = float(cov_m.group(1))
                    agent._test_results.append(tr)
                    if len(agent._test_results) > 50:
                        agent._test_results = agent._test_results[-50:]
                    counts["tests"] += 1
                    break

            # Natural language file operations
            for fre, fop_type in [
                (self._FILE_READ, "read"),
                (self._FILE_WRITE, "write"),
            ]:
                m = fre.search(stripped)
                if m:
                    fop = FileOperation(
                        agent_id=agent.id,
                        file_path=m.group(1),
                        operation=fop_type,
                        timestamp=now,
                    )
                    agent._file_operations.append(fop)
                    if len(agent._file_operations) > 500:
                        agent._file_operations = agent._file_operations[-500:]
                    counts["files"] += 1
                    break

        return counts

    def get_activity_summary(self, agent: "Agent") -> dict:
        """Get structured activity summary for an agent."""
        return {
            "tool_invocations": [t.to_dict() for t in agent._tool_invocations[-50:]],
            "file_operations": [f.to_dict() for f in agent._file_operations[-50:]],
            "git_operations": [g.to_dict() for g in agent._git_operations[-20:]],
            "test_results": [t.to_dict() for t in agent._test_results[-10:]],
            "summary": {
                "total_tools": len(agent._tool_invocations),
                "total_files": len(agent._file_operations),
                "total_git_ops": len(agent._git_operations),
                "total_test_runs": len(agent._test_results),
                "unique_files": len(set(f.file_path for f in agent._file_operations)),
                "tools_by_type": self._count_by_field(agent._tool_invocations, "tool"),
                "files_by_operation": self._count_by_field(agent._file_operations, "operation"),
            },
        }

    @staticmethod
    def _count_by_field(items: list, field_name: str) -> dict:
        counts: dict[str, int] = {}
        for item in items:
            key = getattr(item, field_name, "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts


# Singleton parser instance
_intelligence_parser = OutputIntelligenceParser()


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
                    choices = data.get("choices") or []
                    content = (choices[0].get("message", {}).get("content", "").strip() if choices else "")
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


# ── Anthropic Intelligence Client ──

class AnthropicIntelligenceClient:
    """Claude API client for intelligent summaries, command parsing, and fleet analysis.
    Raw HTTP via aiohttp (no SDK dependency, same pattern as xAI integration).
    """

    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.haiku_model = "claude-haiku-4-5-20251001"  # For fast summaries
        self._session: aiohttp.ClientSession | None = None
        self._failures: int = 0
        self._max_failures: int = 5
        self._circuit_reset_time: float = 0.0
        self.available: bool = True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _check_circuit(self) -> bool:
        """Returns True if we can make a request."""
        if not self.available:
            return False
        if self._failures >= self._max_failures:
            if time.monotonic() < self._circuit_reset_time:
                return False
            self._failures = 0
        return True

    def _handle_error(self, status: int, retry_after: str | None = None) -> None:
        """Handle HTTP error responses."""
        if status in (401, 403):
            log.error(f"Anthropic API disabled: auth failed (HTTP {status})")
            self.available = False
        elif status == 429:
            cooldown = float(retry_after) if retry_after and retry_after.replace('.', '').isdigit() else 60.0
            log.warning(f"Anthropic rate limited, cooling down for {cooldown}s")
            self._circuit_reset_time = time.monotonic() + cooldown
        else:
            self._failures += 1
            if self._failures >= self._max_failures:
                self._circuit_reset_time = time.monotonic() + 60.0
                log.warning("Anthropic circuit breaker tripped, cooling down for 60s")

    async def _call(self, messages: list[dict], model: str | None = None,
                    max_tokens: int = 200, system: str = "") -> str | None:
        """Make a raw API call to Claude. Returns response text or None."""
        if not self._check_circuit():
            return None

        try:
            session = await self._get_session()
            body: dict[str, Any] = {
                "model": model or self.model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                body["system"] = system

            async with session.post(
                f"{self.BASE_URL}/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("content", [])
                    text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
                    self._failures = 0
                    return text.strip() if text else None
                else:
                    self._handle_error(resp.status, resp.headers.get("Retry-After"))
                    return None
        except asyncio.TimeoutError:
            log.debug("Anthropic API request timed out")
            self._failures += 1
            return None
        except Exception as e:
            log.debug(f"Anthropic API error: {e}")
            self._failures += 1
            return None

    async def summarize(self, output_lines: list[str], task: str, role: str, status: str) -> str | None:
        """Generate a 1-line summary. Uses Haiku for speed."""
        recent = output_lines[-40:]
        if not recent:
            return None
        output_text = _strip_ansi("\n".join(recent))[:4000]

        return await self._call(
            messages=[{"role": "user", "content": (
                f"Agent role: {role}\nStatus: {status}\nTask: {task}\n\n"
                f"Recent output:\n```\n{output_text}\n```\n\n"
                f"Write a concise 1-sentence summary (max 80 chars) of what the agent is doing now. "
                f"Focus on the specific action and file/component. No quotes."
            )}],
            model=self.haiku_model,
            max_tokens=60,
        )

    async def parse_command(self, transcript: str, agents: list, context: dict) -> ParsedIntent:
        """Parse a natural language command into a structured intent."""
        agent_list = "\n".join(
            f"- {a.name} (id={a.id}, role={a.role}, status={a.status})"
            for a in agents
        ) or "(no agents running)"

        response = await self._call(
            messages=[{"role": "user", "content": transcript}],
            system=(
                "You are a command parser for an AI agent orchestration platform called Ashlar.\n"
                "Parse the user's natural language command into a JSON intent.\n\n"
                f"Current agents:\n{agent_list}\n\n"
                "Respond with ONLY a JSON object:\n"
                '{"action":"spawn|kill|pause|resume|send|status|query",'
                '"targets":["agent_id1"],'
                '"filter":"optional role/status filter",'
                '"message":"message to send if action=send",'
                '"parameters":{"role":"general","task":"optional task description"},'
                '"confidence":0.0-1.0}'
            ),
            max_tokens=300,
        )

        if response:
            try:
                data = json.loads(response)
                return ParsedIntent(
                    action=data.get("action", "unknown"),
                    targets=data.get("targets", []),
                    filter=data.get("filter", ""),
                    message=data.get("message", ""),
                    parameters=data.get("parameters", {}),
                    confidence=float(data.get("confidence", 0.5)),
                )
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.debug(f"Failed to parse Claude command response: {e}")

        return ParsedIntent(action="unknown", message=transcript, confidence=0.0)

    async def analyze_fleet(self, agents: list, insights_history: list) -> list[AgentInsight]:
        """Meta-agent analysis: detect conflicts, stuck agents, handoff opportunities."""
        if not agents or len(agents) < 2:
            return []

        agent_summaries = []
        for a in agents:
            files = list(set(f.file_path for f in a._file_operations[-20:]))
            tools = list(set(t.tool for t in a._tool_invocations[-20:]))
            agent_summaries.append(
                f"- {a.name} ({a.role}, {a.status}): {a.summary or a.task}\n"
                f"  Files: {', '.join(files[:10]) or 'none'}\n"
                f"  Recent tools: {', '.join(tools) or 'none'}\n"
                f"  Health: {a.health_score:.0%}, context: {a.context_pct:.0%}"
            )

        response = await self._call(
            messages=[{"role": "user", "content": (
                "Analyze these agents and identify issues:\n\n"
                + "\n".join(agent_summaries)
                + "\n\nRespond with a JSON array of insights:\n"
                '[{"type":"conflict|stuck|handoff|anomaly|suggestion",'
                '"severity":"info|warning|critical",'
                '"message":"description",'
                '"agent_ids":["id1"],'
                '"suggested_action":"what to do"}]\n'
                "Only include genuine issues. Return [] if everything looks fine."
            )}],
            max_tokens=500,
        )

        insights = []
        if response:
            try:
                data = json.loads(response)
                if isinstance(data, list):
                    for item in data[:10]:
                        insights.append(AgentInsight(
                            id=uuid.uuid4().hex[:8],
                            insight_type=item.get("type", "suggestion"),
                            severity=item.get("severity", "info"),
                            message=item.get("message", ""),
                            agent_ids=item.get("agent_ids", []),
                            suggested_action=item.get("suggested_action", ""),
                            created_at=time.monotonic(),
                        ))
            except (json.JSONDecodeError, KeyError) as e:
                log.debug(f"Failed to parse fleet analysis response: {e}")

        return insights


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
        if not self._db:
            return
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
        if not self._db:
            return []
        async with self._db.execute(
            "SELECT * FROM agents_history ORDER BY completed_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_agent_history_count(self) -> int:
        if not self._db:
            return 0
        async with self._db.execute("SELECT COUNT(*) FROM agents_history") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_agent_history_item(self, agent_id: str) -> dict | None:
        if not self._db:
            return None
        async with self._db.execute(
            "SELECT * FROM agents_history WHERE id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_historical_analytics(self) -> dict:
        """Compute historical success rates, cost breakdowns, and performance metrics."""
        if not self._db:
            return {"success_rate": {}, "cost_by_role": {}, "cost_by_backend": {}, "performance_by_role": {}, "error_patterns": {}, "total_historical": 0}

        result: dict = {}

        # Success rate by role
        async with self._db.execute(
            """SELECT role,
                      COUNT(*) as total,
                      SUM(CASE WHEN status NOT IN ('error') THEN 1 ELSE 0 END) as successes,
                      AVG(duration_sec) as avg_duration,
                      SUM(estimated_cost_usd) as total_cost
               FROM agents_history GROUP BY role"""
        ) as cur:
            rows = await cur.fetchall()
            by_role = {}
            for r in rows:
                role = r["role"] or "unknown"
                total = r["total"] or 0
                successes = r["successes"] or 0
                by_role[role] = {
                    "total": total,
                    "successes": successes,
                    "success_rate": round(successes / total * 100, 1) if total > 0 else 0,
                    "avg_duration_sec": round(r["avg_duration"] or 0),
                    "total_cost_usd": round(r["total_cost"] or 0, 4),
                }
            result["success_rate_by_role"] = by_role

        # Success rate by backend
        async with self._db.execute(
            """SELECT backend,
                      COUNT(*) as total,
                      SUM(CASE WHEN status NOT IN ('error') THEN 1 ELSE 0 END) as successes,
                      AVG(duration_sec) as avg_duration,
                      SUM(estimated_cost_usd) as total_cost
               FROM agents_history GROUP BY backend"""
        ) as cur:
            rows = await cur.fetchall()
            by_backend = {}
            for r in rows:
                backend = r["backend"] or "unknown"
                total = r["total"] or 0
                successes = r["successes"] or 0
                by_backend[backend] = {
                    "total": total,
                    "successes": successes,
                    "success_rate": round(successes / total * 100, 1) if total > 0 else 0,
                    "avg_duration_sec": round(r["avg_duration"] or 0),
                    "total_cost_usd": round(r["total_cost"] or 0, 4),
                }
            result["success_rate_by_backend"] = by_backend

        # Recent error patterns from activity_events
        async with self._db.execute(
            """SELECT event_type, COUNT(*) as cnt
               FROM activity_events
               WHERE event_type LIKE '%error%' OR event_type LIKE '%fail%'
               GROUP BY event_type ORDER BY cnt DESC LIMIT 10"""
        ) as cur:
            rows = await cur.fetchall()
            result["error_patterns"] = {r["event_type"]: r["cnt"] for r in rows}

        # Total historical
        async with self._db.execute("SELECT COUNT(*) FROM agents_history") as cur:
            row = await cur.fetchone()
            result["total_historical"] = row[0] if row else 0

        # Cost over recent sessions (last 50 agents)
        async with self._db.execute(
            """SELECT role, backend, estimated_cost_usd, duration_sec, status
               FROM agents_history ORDER BY completed_at DESC LIMIT 50"""
        ) as cur:
            rows = await cur.fetchall()
            recent_cost_by_role: dict[str, float] = {}
            recent_cost_by_backend: dict[str, float] = {}
            for r in rows:
                role = r["role"] or "unknown"
                backend = r["backend"] or "unknown"
                cost = r["estimated_cost_usd"] or 0
                recent_cost_by_role[role] = recent_cost_by_role.get(role, 0) + cost
                recent_cost_by_backend[backend] = recent_cost_by_backend.get(backend, 0) + cost
            result["recent_cost_by_role"] = {k: round(v, 4) for k, v in recent_cost_by_role.items()}
            result["recent_cost_by_backend"] = {k: round(v, 4) for k, v in recent_cost_by_backend.items()}

        return result

    async def find_similar_tasks(self, task_query: str, limit: int = 5) -> list[dict]:
        """Find historical agents with similar tasks using keyword matching."""
        if not self._db or not task_query:
            return []
        # Extract keywords (>3 chars, lowercase)
        keywords = [w.lower() for w in task_query.split() if len(w) > 3]
        if not keywords:
            return []
        # Build OR condition for keyword matching
        conditions = " OR ".join(["LOWER(task) LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        params.append(limit * 3)  # fetch more than needed for scoring
        async with self._db.execute(
            f"""SELECT id, name, role, backend, task, status, duration_sec,
                       estimated_cost_usd, context_pct, completed_at
                FROM agents_history
                WHERE {conditions}
                ORDER BY completed_at DESC LIMIT ?""",
            params,
        ) as cur:
            rows = await cur.fetchall()
        # Score by keyword match count
        scored = []
        for r in rows:
            row_task = (r["task"] or "").lower()
            score = sum(1 for kw in keywords if kw in row_task)
            scored.append((score, dict(r)))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:limit]]

    # ── Projects ──

    async def save_project(self, project: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO projects (id, name, path, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project["id"], project["name"], project["path"],
             project.get("description", ""), project.get("created_at", now), now),
        )
        await self._db.commit()

    async def get_projects(self) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute("SELECT * FROM projects ORDER BY name") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def delete_project(self, project_id: str) -> bool:
        if not self._db:
            return False
        async with self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,)) as cur:
            await self._db.commit()
            return cur.rowcount > 0

    # ── Workflows ──

    async def save_workflow(self, workflow: dict) -> None:
        if not self._db:
            return
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
        if not self._db:
            return []
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

    active = sum(1 for a in list(agent_manager.agents.values()) if a.status in ("working", "planning"))

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
        self.max_clients: int = 100
        self._last_sync_time: dict[web.WebSocketResponse, float] = {}

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=1 * 1024 * 1024)
        await ws.prepare(request)

        # Enforce connection limit — reject if at capacity
        if len(self.clients) >= self.max_clients:
            log.warning(f"WebSocket connection limit reached ({self.max_clients}), rejecting new client")
            await ws.close(code=1013, message=b"Too many connections")
            return ws

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
            scanner: ExtensionScanner = request.app.get("extension_scanner")
            extensions_data = scanner.to_dict() if scanner else {"skills": [], "mcp_servers": [], "plugins": [], "scanned_at": ""}
            await ws.send_json({
                "type": "sync",
                "agents": [a.to_dict() for a in list(self.agent_manager.agents.values())],
                "projects": projects,
                "workflows": workflows,
                "presets": presets,
                "config": self.config.to_dict(),
                "backends": backends_info,
                "extensions": extensions_data,
                "queue": [t.to_dict() for t in self.agent_manager.task_queue],
                "db_ready": request.app.get("db_ready", False),
                "db_available": request.app.get("db_available", True),
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
            self._last_sync_time.pop(ws, None)
            log.info(f"WebSocket client disconnected ({len(self.clients)} total)")

        return ws

    async def handle_message(self, data: dict, ws: web.WebSocketResponse) -> None:
        msg_type = data.get("type")

        match msg_type:
            case "spawn":
                task = data.get("task", "")
                if not isinstance(task, str) or not task.strip():
                    await ws.send_json({"type": "error", "message": "task is required"})
                    return
                try:
                    agent = await self.agent_manager.spawn(
                        role=data.get("role", self.config.default_role),
                        name=data.get("name"),
                        working_dir=data.get("working_dir"),
                        task=task,
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
                if agent_id and isinstance(message, str) and message:
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
                if message is not None and not isinstance(message, str):
                    message = None
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
                now = time.monotonic()
                last = self._last_sync_time.get(ws, 0.0)
                if now - last < 2.0:
                    await ws.send_json({"type": "error", "message": "sync_request throttled (max 1 per 2s)"})
                    return
                self._last_sync_time[ws] = now
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
                app = getattr(self, 'app', None)
                scanner2: ExtensionScanner | None = app.get("extension_scanner") if app else None
                ext_data = scanner2.to_dict() if scanner2 else {"skills": [], "mcp_servers": [], "plugins": [], "scanned_at": ""}
                await ws.send_json({
                    "type": "sync",
                    "agents": [a.to_dict() for a in list(self.agent_manager.agents.values())],
                    "projects": projects,
                    "workflows": workflows,
                    "presets": presets,
                    "config": self.config.to_dict(),
                    "backends": backends_info,
                    "extensions": ext_data,
                    "db_ready": app.get("db_ready", False) if app else False,
                    "db_available": app.get("db_available", True) if app else True,
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
    agents = [a.to_dict() for a in list(manager.agents.values())]
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
    if not task.strip():
        return web.json_response({"error": "task is required"}, status=400)
    if len(task) > 10000:
        return web.json_response({"error": "task exceeds 10000 character limit"}, status=400)

    role = data.get("role", request.app["config"].default_role)
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)

    backend = data.get("backend", "claude-code")
    if not isinstance(backend, str):
        return web.json_response({"error": "backend must be a string"}, status=400)
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if backend not in valid_backends:
        return web.json_response({"error": f"Unknown backend '{backend}'. Available: {', '.join(sorted(valid_backends))}"}, status=400)

    working_dir = data.get("working_dir")
    if working_dir is not None and not isinstance(working_dir, str):
        return web.json_response({"error": "working_dir must be a string"}, status=400)
    if working_dir:
        expanded = os.path.expanduser(working_dir)
        if not os.path.isdir(expanded):
            return web.json_response({"error": f"Working directory not found: {working_dir}"}, status=400)

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

    plan_mode = data.get("plan_mode", False)
    if not isinstance(plan_mode, bool):
        return web.json_response({"error": "plan_mode must be a boolean"}, status=400)

    project_id = data.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        return web.json_response({"error": "project_id must be a string"}, status=400)

    try:
        agent = await manager.spawn(
            role=role,
            name=name,
            working_dir=working_dir,
            task=task,
            plan_mode=plan_mode,
            backend=backend,
            model=model_sel,
            tools=tools_sel,
            system_prompt_extra=system_prompt_extra,
            resume_session=resume_session,
        )

        # Assign project after spawn
        if project_id:
            agent.project_id = project_id
        else:
            # Auto-assign project based on working directory match
            db: Database = request.app["db"]
            projects = await db.get_projects()
            best_match = None
            best_len = 0
            for proj in projects:
                proj_path = os.path.abspath(os.path.expanduser(proj.get("path", "")))
                if agent.working_dir.startswith(proj_path) and len(proj_path) > best_len:
                    best_match = proj
                    best_len = len(proj_path)
            if best_match:
                agent.project_id = best_match["id"]

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
    return web.json_response({"error": f"Failed to kill agent '{name}' — tmux session may have already terminated"}, status=500)


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
    if not isinstance(message, str) or not message:
        return web.json_response({"error": "No message provided (must be a string)"}, status=400)
    if len(message) > 50_000:
        return web.json_response({"error": "Message too long (max 50,000 chars)"}, status=400)

    success = await manager.send_message(agent_id, message)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "sent"})
    return web.json_response({"error": f"Failed to send message to '{agent.name}' — agent may have terminated"}, status=500)


async def update_agent_notes(request: web.Request) -> web.Response:
    """PUT /api/agents/{id}/notes — update agent notes."""
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
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        return web.json_response({"error": "Notes must be a string"}, status=400)
    if len(notes) > 10000:
        return web.json_response({"error": "Notes too long (max 10,000 chars)"}, status=400)
    agent.notes = notes
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
    return web.json_response({"status": "updated", "notes": agent.notes})


async def update_agent_tags(request: web.Request) -> web.Response:
    """PUT /api/agents/{id}/tags — update agent tags."""
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
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return web.json_response({"error": "Tags must be a list of strings"}, status=400)
    if len(tags) > 20:
        return web.json_response({"error": "Maximum 20 tags"}, status=400)
    # Sanitize: strip whitespace, lowercase, remove empty, deduplicate
    clean_tags = list(dict.fromkeys(t.strip().lower()[:30] for t in tags if t.strip()))
    agent.tags = clean_tags
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
    return web.json_response({"status": "updated", "tags": agent.tags})


async def add_agent_bookmark(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/bookmarks — add a bookmark to agent output."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    line_idx = data.get("line", 0)
    text = data.get("text", "")
    label = data.get("label", "")
    if not isinstance(line_idx, int) or not isinstance(text, str):
        return web.json_response({"error": "Invalid bookmark data"}, status=400)
    if len(agent.bookmarks) >= 100:
        return web.json_response({"error": "Maximum 100 bookmarks per agent"}, status=400)
    bookmark = {
        "id": uuid.uuid4().hex[:6],
        "line": line_idx,
        "text": text[:200],
        "label": label[:100] if label else "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    agent.bookmarks.append(bookmark)
    return web.json_response({"status": "added", "bookmark": bookmark})


async def delete_agent_bookmark(request: web.Request) -> web.Response:
    """DELETE /api/agents/{id}/bookmarks/{bid} — remove a bookmark."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    bookmark_id = request.match_info["bid"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    before = len(agent.bookmarks)
    agent.bookmarks = [b for b in agent.bookmarks if b.get("id") != bookmark_id]
    if len(agent.bookmarks) == before:
        return web.json_response({"error": "Bookmark not found"}, status=404)
    return web.json_response({"status": "deleted"})


async def list_agent_bookmarks(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/bookmarks — list agent bookmarks."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    return web.json_response({"bookmarks": agent.bookmarks})


async def pause_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    success = await manager.pause(agent_id)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
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
        if message is not None and not isinstance(message, str):
            return web.json_response({"error": "Message must be a string"}, status=400)
    except Exception:
        message = None

    success = await manager.resume(agent_id, message)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
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

    # Optional task override for retry-with-changes
    new_task: str | None = None
    if request.content_type and "json" in request.content_type:
        try:
            body = await request.json()
            new_task = body.get("task") if isinstance(body, dict) else None
            if new_task and not isinstance(new_task, str):
                return web.json_response({"error": "task must be a string"}, status=400)
            if new_task and len(new_task) > 5000:
                return web.json_response({"error": "task too long (max 5000 chars)"}, status=400)
        except Exception:
            pass

    try:
        success = await manager.restart(agent_id, new_task=new_task)
        if success:
            restarted = manager.agents.get(agent_id)
            if restarted:
                await hub.broadcast({"type": "agent_update", "agent": restarted.to_dict()})
                msg = f"Agent {restarted.name} manually restarted (attempt {restarted.restart_count})"
                if new_task:
                    msg += " with modified task"
                await hub.broadcast_event("agent_restarted", msg, agent_id, restarted.name)
                return web.json_response({"status": "restarted", "restart_count": restarted.restart_count, "task_modified": bool(new_task)})
        return web.json_response({"error": "Restart failed"}, status=500)
    except Exception as e:
        log.error(f"Restart endpoint error for {agent_id}: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def system_metrics(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    metrics = await collect_system_metrics(manager)
    data = metrics.to_dict()
    # Service health indicators
    config: Config = request.app["config"]
    data["services"] = {
        "db_available": request.app.get("db_available", True),
        "llm_enabled": config.llm_enabled,
        "bg_task_health": request.app.get("bg_task_health", {}),
    }
    data["server_cwd"] = os.getcwd()
    return web.json_response(data)


async def fleet_analytics(request: web.Request) -> web.Response:
    """GET /api/analytics — fleet-wide performance analytics."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agents = list(manager.agents.values())

    total_cost = sum(a.estimated_cost_usd for a in agents)
    total_tokens_in = sum(a.tokens_input for a in agents)
    total_tokens_out = sum(a.tokens_output for a in agents)
    total_restarts = sum(a.restart_count for a in agents)

    status_counts = {}
    role_counts = {}
    for a in agents:
        status_counts[a.status] = status_counts.get(a.status, 0) + 1
        role_counts[a.role] = role_counts.get(a.role, 0) + 1

    # File activity across all agents
    all_files: dict[str, int] = {}
    for a in agents:
        for fop in a._file_operations[-100:]:
            path = fop.file_path
            all_files[path] = all_files.get(path, 0) + 1
    top_files = sorted(all_files.items(), key=lambda x: -x[1])[:20]

    # Tool usage across all agents
    tool_counts: dict[str, int] = {}
    for a in agents:
        for inv in a._tool_invocations[-200:]:
            tool_counts[inv.tool] = tool_counts.get(inv.tool, 0) + 1

    # Average health score
    health_scores = [a.health_score for a in agents if a.health_score > 0]
    avg_health = sum(health_scores) / len(health_scores) if health_scores else 0

    # Agent lifespans
    now_iso = datetime.now(timezone.utc).isoformat()
    lifespans = []
    for a in agents:
        if a.created_at:
            try:
                created = datetime.fromisoformat(a.created_at.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                lifespans.append(age_min)
            except Exception:
                pass
    avg_lifespan_min = sum(lifespans) / len(lifespans) if lifespans else 0

    # Historical count
    history_count = await db.get_agent_history_count()

    # Historical analytics (success rates, cost breakdowns)
    historical = await db.get_historical_analytics()

    return web.json_response({
        "total_agents": len(agents),
        "total_cost_usd": round(total_cost, 4),
        "total_tokens_input": total_tokens_in,
        "total_tokens_output": total_tokens_out,
        "total_restarts": total_restarts,
        "status_distribution": status_counts,
        "role_distribution": role_counts,
        "top_files": [{"path": p, "count": c} for p, c in top_files],
        "tool_usage": tool_counts,
        "avg_health_score": round(avg_health, 1),
        "avg_lifespan_minutes": round(avg_lifespan_min, 1),
        "historical_agents": history_count,
        "historical": historical,
    })


async def health_check(request: web.Request) -> web.Response:
    """Lightweight health check — no auth required."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    agents = manager.agents
    return web.json_response({
        "status": "ok",
        "agents": len(agents),
        "agents_active": sum(1 for a in agents.values() if a.status in ("working", "planning", "reading")),
        "agents_waiting": sum(1 for a in agents.values() if a.needs_input),
        "db_available": request.app.get("db_available", True),
        "llm_enabled": config.llm_enabled,
        "backends": {k: v.available for k, v in manager.backend_configs.items()},
    })


async def health_detailed(request: web.Request) -> web.Response:
    """GET /api/health/detailed — comprehensive server health and stats."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agents = manager.agents

    uptime_s = time.monotonic() - manager._start_time
    uptime_str = f"{int(uptime_s // 3600)}h{int((uptime_s % 3600) // 60)}m{int(uptime_s % 60)}s"

    total_cost = sum(a.estimated_cost_usd for a in agents.values())
    total_memory = sum(a.memory_mb for a in agents.values())
    total_tokens = sum((a.tokens_input or 0) + (a.tokens_output or 0) for a in agents.values())
    total_tool_invocations = sum(len(a._tool_invocations) for a in agents.values())
    total_file_operations = sum(len(a._file_operations) for a in agents.values())

    status_counts: dict[str, int] = {}
    for a in agents.values():
        status_counts[a.status] = status_counts.get(a.status, 0) + 1

    # Database info
    db = request.app.get("db")
    db_size_mb = 0.0
    if db and hasattr(db, 'db_path'):
        try:
            db_size_mb = Path(db.db_path).stat().st_size / (1024 * 1024)
        except Exception:
            pass

    # Background task health
    bg_tasks = request.app.get("_bg_tasks", {})
    bg_task_status = {}
    for name, task in bg_tasks.items():
        if isinstance(task, asyncio.Task):
            bg_task_status[name] = "running" if not task.done() else "stopped"

    # Process memory (server itself)
    try:
        import psutil
        process = psutil.Process()
        server_memory_mb = process.memory_info().rss / (1024 * 1024)
    except Exception:
        server_memory_mb = 0.0

    return web.json_response({
        "status": "ok",
        "uptime": uptime_str,
        "uptime_seconds": round(uptime_s),
        "server_memory_mb": round(server_memory_mb, 1),
        "agents": {
            "active": len(agents),
            "total_spawned": manager._total_spawned,
            "total_killed": manager._total_killed,
            "total_messages_sent": manager._total_messages_sent,
            "status_breakdown": status_counts,
            "total_agent_memory_mb": round(total_memory, 1),
        },
        "costs": {
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
        },
        "intelligence": {
            "total_tool_invocations": total_tool_invocations,
            "total_file_operations": total_file_operations,
        },
        "websocket": {
            "connected_clients": len(hub.clients),
        },
        "database": {
            "available": request.app.get("db_available", True),
            "size_mb": round(db_size_mb, 2),
        },
        "background_tasks": bg_task_status,
        "llm": {
            "enabled": config.llm_enabled,
            "provider": config.llm_provider if config.llm_enabled else None,
        },
        "backends": {k: {"available": v.available, "command": v.command} for k, v in manager.backend_configs.items()},
        "config": {
            "max_concurrent": config.max_concurrent_agents,
            "port": config.port,
            "cost_budget_usd": config.cost_budget_usd,
        },
    })


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


async def export_agent_output(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/output/export — download agent output as plain text."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    # Collect all output: archive + live
    archived, _ = await db.get_archived_output(agent_id, 0, 100000)
    live = list(agent.output_lines)
    all_lines = archived + live

    # Strip ANSI codes for clean text
    ansi_re = re.compile(r'\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]')
    clean = [ansi_re.sub('', line) for line in all_lines]
    text = "\n".join(clean)

    filename = f"ashlar-{agent.name}-{agent_id}.log"
    return web.Response(
        text=text,
        content_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def search_agents(request: web.Request) -> web.Response:
    """GET /api/search?q=pattern — search across all agent outputs."""
    manager: AgentManager = request.app["agent_manager"]
    query = request.query.get("q", "").strip()
    if not query or len(query) < 2:
        return web.json_response({"error": "Query 'q' must be at least 2 characters"}, status=400)
    if len(query) > 200:
        return web.json_response({"error": "Query too long"}, status=400)

    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:
        return web.json_response({"error": "Invalid search pattern"}, status=400)

    results = []
    for agent in list(manager.agents.values()):
        matches = []
        lines = list(agent.output_lines)
        for i, line in enumerate(lines):
            stripped = _strip_ansi(line) if callable(_strip_ansi) else line
            if pattern.search(stripped):
                matches.append({"line": i, "text": stripped[:200]})
                if len(matches) >= 10:
                    break
        if matches:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "role": agent.role,
                "status": agent.status,
                "match_count": len(matches),
                "matches": matches,
            })

    return web.json_response({"query": query, "results": results, "agents_searched": len(manager.agents)})


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
        "cost_budget_usd": lambda v: isinstance(v, (int, float)) and v >= 0,
        "cost_budget_auto_pause": lambda v: isinstance(v, bool),
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
        "cost_budget_usd": "cost_budget_usd",
        "cost_budget_auto_pause": "cost_budget_auto_pause",
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
    manager: AgentManager = request.app["agent_manager"]
    projects = await db.get_projects()
    # Enrich with agent counts and cost
    for proj in projects:
        agents = [a for a in manager.agents.values() if a.project_id == proj["id"]]
        proj["agent_count"] = len(agents)
        proj["active_count"] = sum(1 for a in agents if a.status in ("working", "planning", "reading"))
        proj["total_cost"] = round(sum(a.estimated_cost_usd for a in agents), 4)
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

    agent_specs = data["agents"]
    if not isinstance(agent_specs, list) or len(agent_specs) == 0:
        return web.json_response({"error": "agents must be a non-empty list"}, status=400)

    # Validate each agent spec
    valid_indices = set(range(len(agent_specs)))
    for i, spec in enumerate(agent_specs):
        if not isinstance(spec, dict):
            return web.json_response({"error": f"agents[{i}] must be an object"}, status=400)
        if not spec.get("role"):
            return web.json_response({"error": f"agents[{i}] missing required field 'role'"}, status=400)
        deps = spec.get("depends_on")
        if deps:
            if not isinstance(deps, list):
                return web.json_response({"error": f"agents[{i}].depends_on must be a list"}, status=400)
            for dep in deps:
                if not isinstance(dep, int) or dep not in valid_indices:
                    return web.json_response({
                        "error": f"agents[{i}].depends_on contains invalid index {dep} (valid: 0-{len(agent_specs)-1})"
                    }, status=400)
                if dep == i:
                    return web.json_response({"error": f"agents[{i}].depends_on cannot reference itself"}, status=400)

    workflow = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"],
        "description": data.get("description", ""),
        "agents_json": agent_specs,
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


# ── Intelligence Endpoints ──

async def get_agent_activity(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/activity — structured tool/file/git/test data."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    activity = _intelligence_parser.get_activity_summary(agent)
    return web.json_response(activity)


async def get_agent_tool_invocations(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/tool-invocations — tool invocation history."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    return web.json_response({
        "agent_id": agent_id,
        "invocations": [t.to_dict() for t in agent._tool_invocations[-limit:]],
        "total": len(agent._tool_invocations),
    })


async def get_agent_file_operations(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/file-operations — file operation history."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    return web.json_response({
        "agent_id": agent_id,
        "operations": [f.to_dict() for f in agent._file_operations[-limit:]],
        "total": len(agent._file_operations),
    })


async def get_agent_snapshots(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/snapshots — output snapshots at key lifecycle points."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    return web.json_response({
        "agent_id": agent_id,
        "snapshots": [s.to_dict() for s in agent._snapshots],
        "total": len(agent._snapshots),
    })


async def create_agent_snapshot(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/snapshots — create a manual snapshot."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    snap = agent.create_snapshot(trigger="manual")
    return web.json_response(snap.to_dict(), status=201)


async def get_intelligence_insights(request: web.Request) -> web.Response:
    """GET /api/intelligence/insights — current cross-agent insights."""
    insights: list[AgentInsight] = request.app.get("intelligence_insights", [])
    return web.json_response({
        "insights": [i.to_dict() for i in insights if not i.acknowledged],
        "total": len(insights),
    })


async def acknowledge_insight(request: web.Request) -> web.Response:
    """POST /api/intelligence/insights/{id}/ack — acknowledge an insight."""
    insights: list[AgentInsight] = request.app.get("intelligence_insights", [])
    insight_id = request.match_info["id"]
    for insight in insights:
        if insight.id == insight_id:
            insight.acknowledged = True
            return web.json_response({"status": "acknowledged"})
    return web.json_response({"error": "Insight not found"}, status=404)


async def intelligence_command(request: web.Request) -> web.Response:
    """POST /api/intelligence/command — parse natural language command via Claude API."""
    client = request.app.get("anthropic_client")
    manager: AgentManager = request.app["agent_manager"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    transcript = data.get("transcript", "").strip()
    if not transcript:
        return web.json_response({"error": "Empty transcript"}, status=400)

    # If Claude API client is available, use it for NLU
    if client and client.available:
        try:
            intent = await client.parse_command(
                transcript,
                list(manager.agents.values()),
                {"total_agents": len(manager.agents)},
            )
            return web.json_response({
                "intent": intent.to_dict(),
                "source": "anthropic",
            })
        except Exception as e:
            log.warning(f"Intelligence command parse failed: {e}")

    # Fallback to keyword matching
    intent = _keyword_parse_command(transcript, list(manager.agents.values()))
    return web.json_response({
        "intent": intent.to_dict(),
        "source": "keyword",
    })


def _keyword_parse_command(transcript: str, agents: list) -> ParsedIntent:
    """Fallback keyword-based command parsing."""
    t = transcript.lower().strip()

    # Spawn patterns
    if any(w in t for w in ("spawn", "start", "create", "launch", "new agent")):
        role = "general"
        for r in BUILTIN_ROLES:
            if r in t:
                role = r
                break
        return ParsedIntent(action="spawn", parameters={"role": role}, confidence=0.6)

    # Kill patterns
    if any(w in t for w in ("kill", "stop", "terminate", "remove")):
        targets = _resolve_agent_refs(t, agents)
        if "all" in t:
            targets = [a.id for a in agents]
        return ParsedIntent(action="kill", targets=targets, confidence=0.6)

    # Pause/resume patterns
    if any(w in t for w in ("pause", "freeze", "hold")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="pause", targets=targets, confidence=0.6)
    if any(w in t for w in ("resume", "unpause", "continue")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="resume", targets=targets, confidence=0.6)

    # Status query
    if any(w in t for w in ("status", "what", "how", "doing")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="status", targets=targets, confidence=0.5)

    # Send message
    if any(w in t for w in ("tell", "send", "approve", "reject", "yes", "no")):
        targets = _resolve_agent_refs(t, agents)
        message = transcript  # Use full transcript as the message
        if "approve" in t:
            message = "yes, proceed"
        elif "reject" in t:
            message = "no, stop"
        return ParsedIntent(action="send", targets=targets, message=message, confidence=0.5)

    return ParsedIntent(action="unknown", message=transcript, confidence=0.2)


def _resolve_agent_refs(text: str, agents: list) -> list[str]:
    """Resolve agent references in natural language text."""
    resolved = []
    for agent in agents:
        if agent.name.lower() in text or agent.id.lower() in text:
            resolved.append(agent.id)
    # Try numeric references ("agent 1", "agent 2")
    import re as _re
    for m in _re.finditer(r'agent\s*(\d+)', text):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(agents):
            resolved.append(agents[idx].id)
    return resolved


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
    for agent in list(manager.agents.values()):
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
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if backend not in valid_backends:
        return web.json_response({"error": f"Invalid backend '{backend}'. Valid backends: {', '.join(sorted(valid_backends))}"}, status=400)

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
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if updated.get("backend") and updated["backend"] not in valid_backends:
        return web.json_response({"error": f"Invalid backend '{updated['backend']}'. Valid backends: {', '.join(sorted(valid_backends))}"}, status=400)

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


# ── Task Queue endpoints ──

async def list_queue(request: web.Request) -> web.Response:
    """GET /api/queue — list all queued tasks."""
    manager: AgentManager = request.app["agent_manager"]
    return web.json_response([t.to_dict() for t in manager.task_queue])


async def add_to_queue(request: web.Request) -> web.Response:
    """POST /api/queue — add a task to the auto-spawn queue."""
    if r := _check_rate(request, cost=2, rate=1, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    role = data.get("role", "general")
    name = data.get("name", f"{role}-{uuid.uuid4().hex[:4]}")
    task_desc = data.get("task", "")
    if not task_desc:
        return web.json_response({"error": "task is required"}, status=400)

    config: Config = request.app["config"]
    queued = QueuedTask(
        id=uuid.uuid4().hex[:8],
        role=role,
        name=name,
        task=task_desc,
        working_dir=data.get("working_dir", config.default_working_dir),
        backend=data.get("backend", config.default_backend),
        plan_mode=data.get("plan_mode", False),
        project_id=data.get("project_id"),
        priority=data.get("priority", 0),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    manager.task_queue.append(queued)
    manager.task_queue.sort(key=lambda t: -t.priority)

    await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
    return web.json_response(queued.to_dict(), status=201)


async def remove_from_queue(request: web.Request) -> web.Response:
    """DELETE /api/queue/{id} — remove a task from the queue."""
    if r := _check_rate(request, cost=1):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    task_id = request.match_info["id"]

    for i, t in enumerate(manager.task_queue):
        if t.id == task_id:
            manager.task_queue.pop(i)
            await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
            return web.json_response({"status": "removed"})
    return web.json_response({"error": "Task not found in queue"}, status=404)


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
    tmp_validate = config_path.with_suffix(".yaml.validate")
    try:
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
    except ValueError as e:
        return web.json_response({"error": f"Invalid config structure: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Config validation failed: {e}"}, status=400)
    finally:
        tmp_validate.unlink(missing_ok=True)

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


# ── Extension Discovery endpoints ──

async def get_extensions(request: web.Request) -> web.Response:
    """GET /api/extensions — return cached extension scan results."""
    if r := _check_rate(request, cost=1):
        return r
    scanner: ExtensionScanner = request.app["extension_scanner"]
    return web.json_response(scanner.to_dict())


async def refresh_extensions(request: web.Request) -> web.Response:
    """POST /api/extensions/refresh — re-scan filesystem for extensions."""
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    scanner: ExtensionScanner = request.app["extension_scanner"]
    # Gather project dirs from DB + active agents
    project_dirs: set[str] = set()
    db: Database = request.app["db"]
    if db:
        try:
            projects = await db.get_projects()
            for p in projects:
                pdir = p.get("path", "")
                if pdir:
                    project_dirs.add(pdir)
        except Exception:
            pass
    manager: AgentManager = request.app["agent_manager"]
    for agent in list(manager.agents.values()):
        if agent.working_dir:
            project_dirs.add(agent.working_dir)
    result = scanner.scan(list(project_dirs))
    return web.json_response(result)


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


# ── File Conflicts endpoint ──

async def list_conflicts(request: web.Request) -> web.Response:
    """GET /api/conflicts — current file conflicts between active agents."""
    manager: AgentManager = request.app["agent_manager"]
    conflicts: list[dict] = []
    seen: set[tuple] = set()
    for file_path, agent_ops in manager.file_activity.items():
        writers = [(aid, op) for aid, op in agent_ops.items()
                   if op == "write" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        if len(writers) < 2:
            continue
        for i, (a1, _) in enumerate(writers):
            for a2, _ in writers[i + 1:]:
                key = tuple(sorted([a1, a2])) + (file_path,)
                if key in seen:
                    continue
                seen.add(key)
                ag1, ag2 = manager.agents[a1], manager.agents[a2]
                conflicts.append({
                    "file_path": file_path,
                    "agents": [
                        {"id": a1, "name": ag1.name, "role": ag1.role, "status": ag1.status},
                        {"id": a2, "name": ag2.name, "role": ag2.role, "status": ag2.status},
                    ],
                    "severity": "conflict",
                })
    # Also include read-write overlaps as warnings
    for file_path, agent_ops in manager.file_activity.items():
        writers = [aid for aid, op in agent_ops.items()
                   if op == "write" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        readers = [aid for aid, op in agent_ops.items()
                   if op == "read" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        for w in writers:
            for r in readers:
                key = tuple(sorted([w, r])) + (file_path,)
                if key in seen:
                    continue
                seen.add(key)
                ag_w, ag_r = manager.agents[w], manager.agents[r]
                conflicts.append({
                    "file_path": file_path,
                    "agents": [
                        {"id": w, "name": ag_w.name, "role": ag_w.role, "status": ag_w.status},
                        {"id": r, "name": ag_r.name, "role": ag_r.role, "status": ag_r.status},
                    ],
                    "severity": "warning",
                })
    # Sort: conflicts first, then warnings
    conflicts.sort(key=lambda c: (0 if c["severity"] == "conflict" else 1, c["file_path"]))
    # File activity summary
    active_files = {}
    for fp, ops in manager.file_activity.items():
        active_agents = [aid for aid in ops if aid in manager.agents]
        if active_agents:
            active_files[fp] = {
                "operations": {aid: ops[aid] for aid in active_agents},
                "agent_count": len(active_agents),
            }
    return web.json_response({
        "conflicts": conflicts,
        "total": len(conflicts),
        "active_files": dict(sorted(active_files.items(), key=lambda x: -x[1]["agent_count"])[:50]),
    })


# ── Global Search endpoint ──

async def global_search(request: web.Request) -> web.Response:
    """GET /api/search?q=term&agent_id=optional — search across all agent outputs."""
    manager: AgentManager = request.app["agent_manager"]
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"error": "q parameter required"}, status=400)
    if len(query) > 500:
        return web.json_response({"error": "query too long (max 500)"}, status=400)

    agent_filter = request.query.get("agent_id", "")
    try:
        max_results = min(int(request.query.get("limit", 100)), 500)
    except ValueError:
        max_results = 100
    case_sensitive = request.query.get("case", "false").lower() == "true"
    use_regex = request.query.get("regex", "false").lower() == "true"

    results: list[dict] = []
    pattern = None
    if use_regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(query, flags)
        except re.error:
            return web.json_response({"error": "invalid regex"}, status=400)

    for agent in list(manager.agents.values()):
        if agent_filter and agent.id != agent_filter:
            continue
        lines = agent.output_lines or []
        for i, line in enumerate(lines):
            if len(results) >= max_results:
                break
            matched = False
            if pattern:
                matched = bool(pattern.search(line))
            elif case_sensitive:
                matched = query in line
            else:
                matched = query.lower() in line.lower()
            if matched:
                # Include context: 1 line before and after
                ctx_before = lines[i - 1] if i > 0 else ""
                ctx_after = lines[i + 1] if i + 1 < len(lines) else ""
                results.append({
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "agent_role": agent.role,
                    "line_index": i,
                    "line": line[:500],
                    "context_before": ctx_before[:500],
                    "context_after": ctx_after[:500],
                })
        if len(results) >= max_results:
            break

    return web.json_response({
        "query": query,
        "results": results,
        "total": len(results),
        "truncated": len(results) >= max_results,
    })


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

    message = data.get("message", "")

    if action not in ("kill", "pause", "resume", "send", "restart"):
        return web.json_response({"error": f"Invalid action '{action}'. Must be 'kill', 'pause', 'resume', 'send', or 'restart'"}, status=400)

    if action == "send" and (not isinstance(message, str) or not message.strip()):
        return web.json_response({"error": "Bulk send requires a non-empty 'message' field"}, status=400)

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

            elif action == "send":
                sanitized = message[:500].replace('\r', '')
                sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)
                ok = await manager.send_message(aid, sanitized)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Send failed"})

            elif action == "restart":
                ok = await manager.restart(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Restart failed"})

        except Exception as e:
            failed_items.append({"id": aid, "error": str(e)})

    return web.json_response({"success": success_ids, "failed": failed_items})


async def batch_spawn(request: web.Request) -> web.Response:
    """POST /api/agents/batch-spawn — spawn multiple agents from a single request.

    Body: {"agents": [{"role": "backend", "name": "api", "task": "...", "working_dir": "...", ...}, ...]}
    Optional: "project_id", "plan_mode" at top level to apply to all agents.
    """
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    config: Config = request.app["config"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    agent_specs = data.get("agents", [])
    if not isinstance(agent_specs, list) or not agent_specs:
        return web.json_response({"error": "agents must be a non-empty list"}, status=400)
    if len(agent_specs) > 10:
        return web.json_response({"error": "Maximum 10 agents per batch"}, status=400)

    shared_project = data.get("project_id")
    shared_plan_mode = data.get("plan_mode", False)
    shared_dir = data.get("working_dir")

    spawned = []
    failed = []

    for i, spec in enumerate(agent_specs):
        if not isinstance(spec, dict):
            failed.append({"index": i, "error": "Each agent spec must be an object"})
            continue
        task = spec.get("task", "")
        if not task:
            failed.append({"index": i, "error": "task is required"})
            continue
        role = spec.get("role", config.default_role)
        if role not in BUILTIN_ROLES:
            failed.append({"index": i, "error": f"Unknown role '{role}'"})
            continue

        try:
            agent = await manager.spawn(
                role=role,
                name=spec.get("name"),
                working_dir=spec.get("working_dir") or shared_dir,
                task=task,
                plan_mode=spec.get("plan_mode", shared_plan_mode),
                backend=spec.get("backend", config.default_backend),
                model=spec.get("model"),
                tools=spec.get("tools"),
            )
            if shared_project or spec.get("project_id"):
                agent.project_id = spec.get("project_id") or shared_project
            spawned.append(agent.to_dict())
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
            await hub.broadcast_event("agent_spawned", f"Batch spawned: {agent.name}", agent.id, agent.name)
        except Exception as e:
            failed.append({"index": i, "error": str(e)})

    return web.json_response({
        "spawned": spawned,
        "failed": failed,
        "total_spawned": len(spawned),
        "total_failed": len(failed),
    }, status=201 if spawned else 400)


async def agent_suggestions(request: web.Request) -> web.Response:
    """GET /api/agents/suggestions?task=... — find similar past tasks with their outcomes."""
    db: Database = request.app["db"]
    task_query = request.query.get("task", "").strip()
    if not task_query or len(task_query) < 5:
        return web.json_response({"suggestions": [], "message": "Provide at least 5 characters in task query"})
    try:
        results = await db.find_similar_tasks(task_query, limit=5)
        suggestions = []
        for r in results:
            suggestions.append({
                "role": r.get("role"),
                "backend": r.get("backend"),
                "task": r.get("task"),
                "status": r.get("status"),
                "duration_sec": r.get("duration_sec"),
                "cost_usd": round(r.get("estimated_cost_usd") or 0, 4),
                "completed_at": r.get("completed_at"),
                "success": r.get("status") not in ("error",),
            })
        return web.json_response({"suggestions": suggestions, "query": task_query})
    except Exception as e:
        log.error(f"Agent suggestions error: {e}")
        return web.json_response({"suggestions": [], "error": str(e)})


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
        if not isinstance(item, dict):
            failed_items.append({"id": "", "error": "Each response must be an object"})
            continue
        aid = item.get("agent_id", "")
        message = item.get("message", "")
        if not isinstance(message, str) or not message:
            failed_items.append({"id": aid, "error": "Missing or invalid message (must be a string)"})
            continue
        if not aid:
            failed_items.append({"id": aid, "error": "Missing agent_id"})
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
    _archive_retry_queue: list[tuple[str, list[str], int]] = []  # (agent_id, lines, offset)
    _seen_conflicts: set[tuple[str, frozenset]] = set()  # Dedup file conflict notifications
    _recent_errors: list[tuple[float, str]] = []  # (timestamp, agent_id) for multi-error detection
    _fleet_error_warned = False

    while True:
        try:
            # Retry previously failed archival (max 5 per iteration)
            if _archive_retry_queue and app.get("db_available", True):
                db_retry: Database = app["db"]
                retried = 0
                still_pending: list[tuple[str, list[str], int]] = []
                for item in _archive_retry_queue:
                    if retried >= 5:
                        still_pending.append(item)
                        continue
                    try:
                        await db_retry.archive_output(item[0], item[1], item[2])
                        retried += 1
                    except Exception:
                        still_pending.append(item)
                        retried += 1
                _archive_retry_queue = still_pending

            # Phase 1: Parallel output capture
            active_agents = [
                (aid, agent) for aid, agent in list(manager.agents.items())
                if agent.status != "paused"
            ]

            # Check spawn timeouts first (cheap, no I/O)
            for agent_id, agent in active_agents:
                if agent.status == "spawning" and agent._spawn_time > 0:
                    if time.monotonic() - agent._spawn_time > 30:
                        agent.set_status("error")
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
                    agent.set_status("error")
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
                    agent._stale_warned = False
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
                                # Queue for retry (cap at 50 to avoid unbounded growth)
                                if len(_archive_retry_queue) < 50:
                                    _archive_retry_queue.append((aid_arch, overflow_lines, offset))

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

                    # File conflict detection (deduplicate — only report each conflict pair+file once)
                    try:
                        conflicts = manager._check_file_conflicts(agent_id, new_lines)
                        for conflict in conflicts:
                            conflict_key = (conflict['file_path'], frozenset([conflict['agent_id'], conflict['other_agent_id']]))
                            if conflict_key not in _seen_conflicts:
                                _seen_conflicts.add(conflict_key)
                                await hub.broadcast_event(
                                    "file_conflict",
                                    f"File conflict: {conflict['agent_name']} and {conflict['other_agent_name']} both working on {conflict['file_path']}",
                                    agent_id, agent.name,
                                    conflict,
                                )
                    except Exception as e:
                        log.debug(f"File conflict detection error for {agent_id}: {e}")

                    # Update summary
                    agent.summary = extract_summary(list(agent.output_lines), agent.task, agent.status)
                    agent.progress_pct = estimate_progress(agent)

                    # Structured intelligence parsing (pure regex, ~0.1ms)
                    try:
                        parse_counts = _intelligence_parser.parse_incremental(agent)
                        if any(parse_counts.values()):
                            activity = _intelligence_parser.get_activity_summary(agent)
                            await self.broadcast({
                                "type": "agent_activity",
                                "agent_id": agent_id,
                                "activity": activity,
                                "new_counts": parse_counts,
                            })
                    except Exception as e:
                        log.debug(f"Intelligence parser error for {agent_id}: {e}")

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
                if agent.status in ("working", "planning") and agent.last_output_time > 0:
                    silence = time.monotonic() - agent.last_output_time
                    if silence > 900:
                        agent.set_status("error")
                        agent.error_message = "No output for 15 minutes"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_stale", f"Agent {agent.name} stale — no output for 15 minutes", agent_id, agent.name)
                    elif silence > 300:
                        if not agent._stale_warned:
                            agent._stale_warned = True
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
                        # Plan approved: clear plan_mode when agent goes waiting → working
                        if agent.plan_mode and old_status == "waiting" and new_status == "working":
                            agent.plan_mode = False
                        agent.set_status(new_status)
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        log.debug(f"Agent {agent_id} status: {old_status} -> {new_status}")

                        # Auto-snapshot on key status transitions
                        if new_status in ("error", "waiting", "idle"):
                            try:
                                agent.create_snapshot(trigger=new_status if new_status != "idle" else "complete")
                            except Exception as snap_err:
                                log.debug(f"Snapshot creation failed for {agent_id}: {snap_err}")

                        if new_status == "error" and old_status != "error":
                            agent._error_entered_at = time.monotonic()
                            # Reset health warning flags so they can re-fire on recovery+relapse
                            agent._health_low_warned = False
                            agent._health_critical_warned = False
                            # Track for multi-error detection
                            now_mono = time.monotonic()
                            _recent_errors.append((now_mono, agent_id))
                            _recent_errors[:] = [(t, a) for t, a in _recent_errors if now_mono - t < 60]
                            if len(_recent_errors) >= 2 and not _fleet_error_warned:
                                err_names = [manager.agents[a].name for _, a in _recent_errors if a in manager.agents]
                                _fleet_error_warned = True
                                await hub.broadcast_event(
                                    "fleet_multi_error",
                                    f"Multiple agents errored: {', '.join(err_names)}",
                                    agent_id, agent.name,
                                )
                            elif len(_recent_errors) < 2:
                                _fleet_error_warned = False
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

                            # Broadcast completion event with summary
                            duration = time.monotonic() - agent._spawn_time if agent._spawn_time else 0
                            dur_str = f"{int(duration // 60)}m{int(duration % 60)}s" if duration > 60 else f"{int(duration)}s"
                            cost_str = f"${agent.estimated_cost_usd:.2f}" if agent.estimated_cost_usd > 0 else ""
                            completion_msg = f"Agent {agent.name} completed"
                            if agent.summary:
                                completion_msg += f": {agent.summary}"
                            await hub.broadcast_event(
                                "agent_completed",
                                completion_msg,
                                agent_id, agent.name,
                                metadata={"duration": dur_str, "cost": cost_str, "summary": agent.summary or ""},
                            )

                            # Suggest follow-up agents based on what this agent did
                            suggestion = _suggest_followup(agent)
                            if suggestion:
                                await hub.broadcast_event(
                                    "agent_followup_suggestion",
                                    suggestion["message"],
                                    agent_id, agent.name,
                                    metadata=suggestion,
                                )

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

            # Prune _seen_conflicts for agents that no longer exist
            active_ids = set(manager.agents.keys())
            _seen_conflicts = {k for k in _seen_conflicts if k[1] <= active_ids}

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
                    agent.set_status("error")
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
                        agent.set_status("error")
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

                # -- Context exhaustion auto-snapshot + warning --
                if agent.context_pct >= 0.92 and agent.status in ("working", "planning", "reading"):
                    if not getattr(agent, '_context_exhaustion_warned', False):
                        agent._context_exhaustion_warned = True
                        log.warning(f"Agent {agent_id} ({agent.name}) context at {agent.context_pct:.0%} — creating snapshot")
                        try:
                            agent.create_snapshot(trigger="context_warning")
                        except Exception:
                            pass
                        await hub.broadcast_event(
                            "agent_context_warning",
                            f"Agent {agent.name} context window at {agent.context_pct:.0%} — approaching limit",
                            agent_id, agent.name,
                        )

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

        # Fleet-wide cost limit check
        try:
            config: Config = app["config"]
            if config.cost_budget_usd > 0 and config.cost_budget_auto_pause:
                fleet_cost = sum(a.estimated_cost_usd for a in list(manager.agents.values()))
                if fleet_cost >= config.cost_budget_usd:
                    if not getattr(app, '_fleet_budget_warned', False):
                        app['_fleet_budget_warned'] = True
                        log.warning(f"Fleet cost ${fleet_cost:.2f} exceeds budget ${config.cost_budget_usd:.2f} — auto-pausing working agents")
                        working = [a for a in list(manager.agents.values()) if a.status in ("working", "planning", "reading")]
                        for a in working:
                            try:
                                await manager.pause(a.id)
                                await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})
                            except Exception as pe:
                                log.warning(f"Failed to auto-pause agent {a.id}: {pe}")
                        await hub.broadcast_event(
                            "fleet_budget_exceeded",
                            f"Fleet cost ${fleet_cost:.2f} exceeds budget ${config.cost_budget_usd:.2f}. {len(working)} agents auto-paused.",
                            None, None,
                        )
        except Exception as e:
            log.error(f"Fleet budget check error: {e}")

        # Auto-spawn from task queue when slots are available
        try:
            config: Config = app["config"]
            active_count = sum(1 for a in list(manager.agents.values()) if a.status not in ("idle", "complete"))
            while manager.task_queue and active_count < config.max_agents:
                queued = manager.task_queue.pop(0)
                try:
                    agent = await manager.spawn(
                        role=queued.role,
                        name=queued.name,
                        working_dir=queued.working_dir,
                        task=queued.task,
                        backend=queued.backend or config.default_backend,
                        plan_mode=queued.plan_mode,
                    )
                    if queued.project_id:
                        agent.project_id = queued.project_id
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast_event(
                        "agent_spawned",
                        f"Auto-spawned from queue: {agent.name}",
                        agent.id, agent.name,
                    )
                    await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
                    active_count += 1
                    log.info(f"Auto-spawned queued task: {queued.name} (role={queued.role})")
                except Exception as e:
                    log.warning(f"Failed to auto-spawn queued task {queued.name}: {e}")
        except Exception as e:
            log.debug(f"Queue auto-spawn check error: {e}")

        await asyncio.sleep(5.0)


async def memory_watchdog_loop(app: web.Application) -> None:
    """Check per-agent memory usage, warn/kill if over limit. Also cleans old archive rows."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    limit = app["config"].memory_limit_mb
    warn_threshold = limit * 0.75
    _cleanup_counter = 0

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

        # Archive cleanup: every ~180 iterations (30 min at 10s interval)
        _cleanup_counter += 1
        if _cleanup_counter >= 180:
            _cleanup_counter = 0
            try:
                db: Database = app["db"]
                if app.get("db_available", True) and db._db:
                    cutoff = datetime.now(timezone.utc).replace(microsecond=0)
                    cutoff_str = cutoff.isoformat().replace("+00:00", "Z")
                    # Delete archive rows older than 24h for agents no longer running
                    active_ids = set(manager.agents.keys())
                    async with db._db.execute(
                        "SELECT DISTINCT agent_id FROM agent_output_archive WHERE created_at < datetime('now', '-24 hours')"
                    ) as cursor:
                        old_agents = [row[0] async for row in cursor]
                    for aid in old_agents:
                        if aid not in active_ids:
                            await db._db.execute("DELETE FROM agent_output_archive WHERE agent_id = ?", (aid,))
                    await db._db.commit()
                    if old_agents:
                        cleaned = [a for a in old_agents if a not in active_ids]
                        if cleaned:
                            log.info(f"Archive cleanup: removed output for {len(cleaned)} old agents")
            except Exception as e:
                log.warning(f"Archive cleanup error: {e}")

        await asyncio.sleep(10.0)


# ── Meta-Agent Intelligence Loop ──

async def meta_agent_loop(app: web.Application) -> None:
    """5th supervised background task: cross-agent analysis via Claude API.
    Runs on configurable interval (default 30s). Detects file conflicts, stuck
    agents, handoff opportunities, and anomalies.
    """
    config: Config = app["config"]
    interval = config.intelligence_meta_interval

    while True:
        await asyncio.sleep(interval)

        client: AnthropicIntelligenceClient | None = app.get("anthropic_client")
        if not client or not client.available:
            continue

        manager: AgentManager = app["agent_manager"]
        agents = list(manager.agents.values())
        if len(agents) < 2:
            continue

        # Hash state to skip re-analysis when nothing changed
        state_parts = [f"{a.id}:{a.status}:{a.summary[:30]}" for a in agents]
        state_hash = str(hash(tuple(state_parts)))
        if state_hash == app.get("_meta_state_hash", ""):
            continue
        app["_meta_state_hash"] = state_hash

        try:
            insights = await client.analyze_fleet(
                agents, app.get("intelligence_insights", [])
            )
            if insights:
                existing: list[AgentInsight] = app.get("intelligence_insights", [])
                existing.extend(insights)
                # Cap at 100 insights
                if len(existing) > 100:
                    app["intelligence_insights"] = existing[-100:]

                hub: WebSocketHub = app["ws_hub"]
                for insight in insights:
                    await hub.broadcast({
                        "type": "intelligence_alert",
                        "insight": insight.to_dict(),
                    })
                log.info(f"Meta-agent generated {len(insights)} insight(s)")
        except Exception as e:
            log.debug(f"Meta-agent analysis error: {e}")


# ─────────────────────────────────────────────
# Section 10: Application Setup & Main
# ─────────────────────────────────────────────

async def _supervised_task(name: str, coro_fn, app: web.Application) -> None:
    """Supervisor wrapper: restarts a background task if it crashes."""
    restart_count = 0
    while True:
        try:
            await coro_fn(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            restart_count += 1
            log.error(f"Background task '{name}' crashed (restart #{restart_count}): {e}")
            # Track health for /api/system
            task_health = app.setdefault("bg_task_health", {})
            task_health[name] = {
                "restarts": restart_count,
                "last_crash": datetime.now(timezone.utc).isoformat(),
                "last_error": str(e)[:200],
            }
            # Broadcast crash event to connected clients
            hub: WebSocketHub | None = app.get("ws_hub")
            if hub:
                try:
                    await hub.broadcast({
                        "type": "event",
                        "event": "bg_task_crash",
                        "task": name,
                        "restart": restart_count,
                        "error": str(e)[:200],
                    })
                except Exception:
                    pass  # Don't let broadcast failure block restart
            await asyncio.sleep(min(5 * restart_count, 30))  # back off


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

    app["db_ready"] = True
    app["bg_task_health"] = {}
    bg_tasks = [
        asyncio.create_task(_supervised_task("output_capture", output_capture_loop, app)),
        asyncio.create_task(_supervised_task("metrics", metrics_loop, app)),
        asyncio.create_task(_supervised_task("health_check", health_check_loop, app)),
        asyncio.create_task(_supervised_task("memory_watchdog", memory_watchdog_loop, app)),
    ]
    # Meta-agent intelligence task (only if Anthropic API is available)
    if app.get("anthropic_client"):
        bg_tasks.append(asyncio.create_task(_supervised_task("meta_agent", meta_agent_loop, app)))
    app["bg_tasks"] = bg_tasks
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
    if app.get("db_available", True):
        for agent in list(manager.agents.values()):
            try:
                await db.save_agent(agent)
            except Exception as e:
                log.warning(f"Shutdown: failed to archive agent {agent.id} ({agent.name}): {e}")

    # Close LLM summarizer
    summarizer: LLMSummarizer | None = app.get("llm_summarizer")
    if summarizer:
        await summarizer.close()

    # Close Anthropic client
    anthropic_client: AnthropicIntelligenceClient | None = app.get("anthropic_client")
    if anthropic_client:
        await anthropic_client.close()

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
    hub.app = app
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

    # Anthropic Intelligence Client
    anthropic_client = None
    if config.intelligence_enabled and config.intelligence_api_key:
        anthropic_client = AnthropicIntelligenceClient(
            api_key=config.intelligence_api_key,
            model=config.intelligence_model,
        )
        log.info(f"Intelligence enabled: Anthropic/{config.intelligence_model}")
    else:
        log.info("Intelligence disabled (set ANTHROPIC_API_KEY env var)")
    app["anthropic_client"] = anthropic_client
    app["intelligence_insights"] = []  # list[AgentInsight]
    app["_meta_state_hash"] = ""  # For skipping unchanged fleet analysis

    # Extension Scanner — initial scan at startup
    scanner = ExtensionScanner()
    scanner.scan()  # Initial scan with no project dirs (DB not ready yet)
    app["extension_scanner"] = scanner

    # Routes
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/logo.png", serve_logo)
    app.router.add_get("/ws", hub.handle_ws)

    # REST API — Agents (bulk + patch BEFORE {id} catch-all routes)
    app.router.add_get("/api/agents", list_agents)
    app.router.add_post("/api/agents", spawn_agent)
    app.router.add_post("/api/agents/bulk", bulk_agent_action)
    app.router.add_post("/api/agents/bulk-respond", bulk_respond)
    app.router.add_post("/api/agents/batch-spawn", batch_spawn)
    app.router.add_get("/api/agents/suggestions", agent_suggestions)
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
    app.router.add_put("/api/agents/{id}/notes", update_agent_notes)
    app.router.add_put("/api/agents/{id}/tags", update_agent_tags)
    app.router.add_get("/api/agents/{id}/bookmarks", list_agent_bookmarks)
    app.router.add_post("/api/agents/{id}/bookmarks", add_agent_bookmark)
    app.router.add_delete("/api/agents/{id}/bookmarks/{bid}", delete_agent_bookmark)
    app.router.add_get("/api/agents/{id}/activity", get_agent_activity)
    app.router.add_get("/api/agents/{id}/tool-invocations", get_agent_tool_invocations)
    app.router.add_get("/api/agents/{id}/file-operations", get_agent_file_operations)
    app.router.add_get("/api/agents/{id}/output/export", export_agent_output)
    app.router.add_get("/api/agents/{id}/snapshots", get_agent_snapshots)
    app.router.add_post("/api/agents/{id}/snapshots", create_agent_snapshot)

    # REST API — Search
    app.router.add_get("/api/search", search_agents)

    # REST API — Intelligence
    app.router.add_get("/api/intelligence/insights", get_intelligence_insights)
    app.router.add_post("/api/intelligence/insights/{id}/ack", acknowledge_insight)
    app.router.add_post("/api/intelligence/command", intelligence_command)

    # REST API — Analytics
    app.router.add_get("/api/analytics", fleet_analytics)

    # REST API — System
    app.router.add_get("/api/health", health_check)
    app.router.add_get("/api/health/detailed", health_detailed)
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
    app.router.add_get("/api/conflicts", list_conflicts)
    app.router.add_get("/api/search", global_search)

    # REST API — Presets
    app.router.add_get("/api/presets", list_presets)
    app.router.add_post("/api/presets", create_preset)
    app.router.add_put("/api/presets/{id}", update_preset)
    app.router.add_delete("/api/presets/{id}", delete_preset)

    # REST API — Agent Clone
    app.router.add_post("/api/agents/{id}/clone", clone_agent)

    # REST API — Task Queue
    app.router.add_get("/api/queue", list_queue)
    app.router.add_post("/api/queue", add_to_queue)
    app.router.add_delete("/api/queue/{id}", remove_from_queue)

    # REST API — Scratchpad
    app.router.add_get("/api/scratchpad", get_scratchpad)
    app.router.add_put("/api/scratchpad", upsert_scratchpad)
    app.router.add_delete("/api/scratchpad/{key}", delete_scratchpad_entry)

    # REST API — Config Import/Export
    app.router.add_get("/api/config/export", export_config)
    app.router.add_post("/api/config/import", import_config)

    # REST API — Extensions
    app.router.add_get("/api/extensions", get_extensions)
    app.router.add_post("/api/extensions/refresh", refresh_extensions)

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
    manager = app["agent_manager"]
    setup_signal_handlers(manager)

    # Clean up orphaned tmux sessions from prior ungraceful shutdowns
    orphans = manager.cleanup_orphaned_sessions()
    if orphans:
        print(f"  \033[33mCleaned up {orphans} orphaned tmux session{'s' if orphans != 1 else ''}\033[0m")

    mode_str = "\033[33mDEMO MODE\033[0m" if config.demo_mode else "\033[32mLIVE MODE\033[0m"
    print(f"  Mode: {mode_str}")
    print(f"  Dashboard: \033[36mhttp://{config.host}:{config.port}\033[0m")
    print(f"  Max agents: {config.max_agents}")
    print()

    try:
        web.run_app(app, host=config.host, port=config.port, print=None)
    except OSError as e:
        if "Address already in use" in str(e) or getattr(e, 'errno', None) == 48:
            log.error(f"Port {config.port} is already in use.")
            log.error(f"  → Set ASHLAR_PORT=8080 to use a different port")
            log.error(f"  → On macOS, AirPlay Receiver may use port 5000")
            log.error(f"    System Settings > General > AirDrop & Handoff > AirPlay Receiver → off")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
