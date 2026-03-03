#!/usr/bin/env python3
"""
Ashlr AO — Agent Orchestration Server

aiohttp server that manages AI coding agents via tmux,
serves the web dashboard, and provides REST + WebSocket APIs.
"""

# ─────────────────────────────────────────────
# Section 1: Imports
# ─────────────────────────────────────────────

import argparse
import asyncio
import collections
import hmac
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
from aiohttp import web
# aiohttp_cors removed — CORS handled natively in security_headers_middleware
import bcrypt
import jwt
import psutil
import yaml

# Re-export from constants module (backward compat)
from ashlr_ao.constants import (  # noqa: F401
    ASHLR_DIR,
    LOG_COLORS,
    RESET,
    ColoredFormatter,
    setup_logging,
    log,
    _ANSI_ESCAPE_RE,
    _strip_ansi,
    _SECRET_PATTERNS,
    redact_secrets,
    print_banner,
    check_dependencies,
)


# Re-export from config module (backward compat)
from ashlr_ao.config import (  # noqa: F401
    DEFAULT_CONFIG,
    deep_merge,
    Config,
    load_config,
)

# Re-export from licensing module (backward compat)
from ashlr_ao.licensing import (  # noqa: F401
    PRO_FEATURES,
    LICENSE_PUBLIC_KEY_PEM,
    License,
    COMMUNITY_LICENSE,
    validate_license,
    _effective_max_agents,
    _check_feature,
)

# Re-export from backends module (backward compat)
from ashlr_ao.backends import (  # noqa: F401
    BackendConfig,
    KNOWN_BACKENDS,
)

# Re-export from roles module (backward compat)
from ashlr_ao.roles import (  # noqa: F401
    Role,
    BUILTIN_ROLES,
)

# Re-export from extensions module (backward compat)
from ashlr_ao.extensions import (  # noqa: F401
    SkillInfo,
    MCPServerInfo,
    PluginInfo,
    ExtensionScanner,
)

# Re-export from intelligence module (backward compat)
from ashlr_ao.intelligence import (  # noqa: F401
    OutputIntelligenceParser,
    _intelligence_parser,
    _alert_throttle,
    IntelligenceClient,
    PHASE_PATTERNS,
    PHASE_PROGRESS,
    detect_phase,
    estimate_progress,
    calculate_health_score,
)

# Re-export from status module (backward compat)
from ashlr_ao.status import (  # noqa: F401
    STATUS_PATTERNS,
    WAITING_LINE_PATTERNS,
    FOLLOWUP_SUGGESTIONS,
    _suggest_followup,
    parse_agent_status,
    _extract_question,
    _FILE_PATH_RE,
    _TEST_RESULT_RE,
    _COVERAGE_RE,
    _FILES_PROGRESS_RE,
    _ACTION_PATTERNS,
    _INTENT_RE,
    _GIT_COMMIT_RE,
    extract_summary,
)

# Re-export from database module (backward compat)
from ashlr_ao.database import Database  # noqa: F401

# Re-export from websocket module (backward compat)
from ashlr_ao.websocket import (  # noqa: F401
    WebSocketHub,
    collect_system_metrics,
)

# Re-export from background module (backward compat)
from ashlr_ao.background import (  # noqa: F401
    output_capture_loop,
    metrics_loop,
    health_check_loop,
    memory_watchdog_loop,
    meta_agent_loop,
    archive_cleanup_loop,
    _supervised_task,
    start_background_tasks,
    cleanup_background_tasks,
)

# Re-export from middleware module (backward compat)
from ashlr_ao.middleware import (  # noqa: F401
    RateLimiter,
    _get_client_ip,
    _RATE_LIMIT_TIERS,
    _get_rate_tier,
    rate_limit_middleware,
    request_logging_middleware,
    compression_middleware,
    security_headers_middleware,
)

# Re-export from models module (backward compat)
from ashlr_ao.models import (  # noqa: F401
    OutputSnapshot,
    Organization,
    User,
    Agent,
    SystemMetrics,
    ToolInvocation,
    FileOperation,
    GitOperation,
    AgentTestResult,
    AgentInsight,
    QueuedTask,
    ParsedIntent,
    WorkflowRun,
    calculate_efficiency_score,
)


# Data models moved to ashlr_ao.models — re-exported above


class AgentManager:
    def __init__(self, config: Config):
        self.config = config
        self.agents: dict[str, Agent] = {}
        self.tmux_prefix = "ashlr"
        self._loop: asyncio.AbstractEventLoop | None = None
        self.db: "Database | None" = None  # Set after creation by create_app()
        self.license: License = COMMUNITY_LICENSE
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
        self._total_api_requests: int = 0

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
        # Claude Code's status bar shows permission mode and help hints.
        # Avoid ❯ or > as they can appear during partial renders.
        ready_patterns = [
            "bypass permissions",     # --dangerously-skip-permissions mode
            "bypassPermissions",      # --permission-mode bypassPermissions
            "Enter a prompt",         # prompt input area
            "shift+tab to cycle",     # help hint in status bar
            "permission-mode",        # any --permission-mode value
            "plan mode",              # --permission-mode plan
            "? for shortcuts",        # help hint variant
        ]
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
        except (ValueError, Exception) as e:
            log.debug(f"Failed to get tmux pane PID for {session}: {e}")
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

    @staticmethod
    def _build_backend_command(
        bc: BackendConfig,
        role: str,
        task: str,
        plan_mode: bool = False,
        model: str | None = None,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_session: str | None = None,
    ) -> tuple[list[str], str]:
        """Build CLI command parts and task text for a backend.

        Returns (cmd_parts, task_to_send) where cmd_parts is the full
        command argument list and task_to_send is the task text (possibly
        with plan-mode prefix).
        """
        cmd_parts = [bc.command]
        cmd_parts.extend(bc.args)
        if bc.auto_approve_flag and bc.auto_approve_flag not in bc.args:
            cmd_parts.append(bc.auto_approve_flag)

        if model and bc.supports_model_select:
            cmd_parts.extend(["--model", model])
        if tools and bc.supports_tool_restriction:
            cmd_parts.extend(["--allowedTools", ",".join(tools)])

        # Plan mode: use backend-specific plan flag (incompatible with auto-approve)
        if plan_mode and bc.plan_mode_flag:
            if bc.auto_approve_flag and bc.auto_approve_flag in cmd_parts:
                cmd_parts.remove(bc.auto_approve_flag)
            cmd_parts.extend(bc.plan_mode_flag.split())

        # System prompt injection
        if system_prompt and bc.supports_system_prompt:
            cmd_parts.extend(["--append-system-prompt", system_prompt])

        # Session resume
        if resume_session and bc.supports_session_resume:
            cmd_parts.extend(["--resume", resume_session])

        # Prepare task text — plan-mode prefix for backends without native plan flag
        if plan_mode and not bc.plan_mode_flag:
            task_to_send = (
                "IMPORTANT: Create a detailed plan for the following task. "
                "DO NOT execute any changes. Present your plan and ask for approval.\n\n"
                f"Task: {task}"
            )
        else:
            task_to_send = task

        # Include task as CLI positional arg if supported (must be last)
        if bc.supports_prompt_arg and task_to_send:
            cmd_parts.append(task_to_send)

        return cmd_parts, task_to_send

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
        max_a = min(self.config.max_agents, self.license.max_agents) if self.license.is_pro else min(self.config.max_agents, COMMUNITY_LICENSE.max_agents)
        if len(self.agents) >= max_a:
            suffix = " — upgrade to Pro for more" if not self.license.is_pro else ""
            raise ValueError(f"Maximum agents ({max_a}) reached{suffix}")

        # System pressure check — block spawning if system is overloaded
        if self.config.spawn_pressure_block:
            pressure = self.check_system_pressure()
            if pressure.get("cpu_pressure") or pressure.get("memory_pressure"):
                reasons = []
                if pressure.get("cpu_pressure"):
                    reasons.append(f"CPU at {pressure.get('cpu_pct', 0):.0f}%")
                if pressure.get("memory_pressure"):
                    reasons.append(f"Memory at {pressure.get('memory_pct', 0):.0f}%")
                raise ValueError(f"System under pressure ({', '.join(reasons)}), spawning blocked. Lower load or disable spawn_pressure_block in config.")

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
            config_dirs = [str(ASHLR_DIR)]
            allowed_prefixes = [home_dir] + config_dirs
            if not any(working_dir.startswith(prefix) for prefix in allowed_prefixes):
                raise ValueError(
                    f"Working directory '{working_dir}' is outside allowed paths. "
                    f"Must be under home directory or Ashlr config dir."
                )

        # Generate ID
        agent_id = uuid.uuid4().hex[:4]
        while agent_id in self.agents:
            agent_id = uuid.uuid4().hex[:4]

        # Generate name if not provided
        if not name:
            role_name = BUILTIN_ROLES.get(role, BUILTIN_ROLES["general"]).name.split()[0].lower()
            name = f"{role_name}-{agent_id}"

        # Detect name collisions — append agent ID suffix if collision found
        existing_names = {a.name for a in list(self.agents.values())}
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
        # Apply configurable output buffer size
        if self.config.output_max_lines != 2000:
            agent.output_lines = collections.deque(maxlen=self.config.output_max_lines)
        agent.last_output_time = time.monotonic()
        self.agents[agent_id] = agent
        self._total_spawned += 1

        # Detect initial git branch from working directory
        try:
            loop = asyncio.get_running_loop()
            branch = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: subprocess.run(
                    ["git", "-C", working_dir, "branch", "--show-current"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip()),
                timeout=5.0,
            )
            if branch:
                agent.git_branch = branch
        except Exception:
            pass  # Not a git repo or git not available

        # Pre-flight: kill any orphaned tmux session with this name (rare UUID collision)
        try:
            check = await self._run_tmux(["has-session", "-t", session_name])
            if check.returncode == 0:
                log.warning(f"Orphaned tmux session {session_name} found, killing before spawn")
                await self._run_tmux(["kill-session", "-t", session_name])
        except Exception:
            pass  # has-session returns non-zero if session doesn't exist — expected

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
                except Exception as cleanup_err:
                    log.warning(f"Failed to cleanup tmux session {session_name}: {cleanup_err}")
                # Remove zombie agent from dict (skip in demo mode — tmux expected to fail)
                if not self.config.demo_mode:
                    self.agents.pop(agent_id, None)
                return agent
        except Exception as e:
            agent.status = "error"
            agent.error_message = str(e)
            try:
                await self._run_tmux(["kill-session", "-t", session_name])
            except Exception as cleanup_err:
                log.warning(f"Failed to cleanup tmux session {session_name}: {cleanup_err}")
            # Remove zombie agent from dict (skip in demo mode — tmux expected to fail)
            if not self.config.demo_mode:
                self.agents.pop(agent_id, None)
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
                    except Exception as cleanup_err:
                        log.warning(f"Failed to remove script {agent.script_path}: {cleanup_err}")
                # Kill orphaned tmux session
                try:
                    await self._run_tmux(["kill-session", "-t", session_name])
                except Exception as cleanup_err:
                    log.warning(f"Failed to cleanup tmux session {session_name}: {cleanup_err}")
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
                    except Exception as cleanup_err:
                        log.warning(f"Failed to cleanup tmux session {session_name}: {cleanup_err}")
                    return agent

            # Build system prompt from role + extra
            role_obj = BUILTIN_ROLES.get(role)
            if not role_obj:
                log.warning(f"Role '{role}' not in BUILTIN_ROLES, falling back to 'general'")
                role_obj = BUILTIN_ROLES["general"]
            sys_parts = []
            if bc.inject_role_prompt and role != "general":
                sys_parts.append(f"You are a {role_obj.name}. {role_obj.system_prompt}")
            if system_prompt_extra:
                sys_parts.append(system_prompt_extra)
            full_system = "\n\n".join(sys_parts)
            if full_system:
                agent.system_prompt = full_system
            if resume_session:
                agent.session_id = resume_session

            cmd_parts, task_to_send = self._build_backend_command(
                bc, role, task,
                plan_mode=plan_mode, model=model, tools=tools,
                system_prompt=full_system, resume_session=resume_session,
            )

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
            f'echo "│ [{role_obj.icon}] Ashlr Agent (Demo Mode)          │"',
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
        script_path = Path(tempfile.gettempdir()) / f"ashlr_demo_{uuid.uuid4().hex[:8]}.sh"
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

        # Check if restart is in progress — wait for it to finish
        if getattr(agent, '_restart_in_progress', False):
            log.warning(f"Kill requested for agent {agent_id} during restart — waiting for restart to complete")
            for _ in range(30):  # Wait up to 3s
                await asyncio.sleep(0.1)
                if not getattr(agent, '_restart_in_progress', False):
                    break

        # Mark as killed immediately so background loops skip this agent
        agent.status = "killed"

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

        # Release DB file locks held by this agent
        try:
            if self.db and hasattr(self.db, 'release_file_locks'):
                await self.db.release_file_locks(agent_id)
        except Exception as e:
            log.warning(f"Failed to release DB file locks for agent {agent_id}: {e}")

        # Guard deletion — agent may have been removed by concurrent operation
        if agent_id in self.agents:
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

        # Prevent concurrent restarts with async lock
        if agent._restart_lock.locked():
            log.warning(f"Restart already in progress for agent {agent_id}")
            return False

        async with agent._restart_lock:
            agent._restart_in_progress = True
            return await self._restart_impl(agent_id, agent, new_task)

    async def _restart_impl(self, agent_id: str, agent: "Agent", new_task: str | None = None) -> bool:
        """Internal restart implementation. Must be called under agent._restart_lock."""
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
            agent._last_parse_index = 0
            agent._total_lines_added = 0
            agent.context_pct = 0.0
            agent.tokens_input = 0
            agent.tokens_output = 0
            agent.estimated_cost_usd = 0.0
            agent.files_touched = 0
            agent._files_touched_set.clear()
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

                role_obj = BUILTIN_ROLES.get(saved_role, BUILTIN_ROLES["general"])
                cmd_parts, task_to_send = self._build_backend_command(
                    bc, saved_role, saved_task,
                    plan_mode=saved_plan_mode, model=saved_model, tools=saved_tools,
                    system_prompt=saved_system_prompt,
                )

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

    @staticmethod
    def _is_binary_garbage(lines: list[str], threshold: float = 0.3) -> bool:
        """Check if output lines contain mostly binary/non-printable garbage.
        Returns True if >threshold fraction of chars are non-printable (excluding common ANSI)."""
        if not lines:
            return False
        sample = "\n".join(lines[-20:])  # Check last 20 lines
        if not sample:
            return False
        # Strip common ANSI escape sequences before checking
        clean = _ANSI_ESCAPE_RE.sub('', sample)
        if not clean:
            return False
        non_printable = sum(1 for c in clean if not c.isprintable() and c not in '\n\r\t')
        return (non_printable / len(clean)) > threshold

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

        # Binary/garbage detection — skip corrupt output to prevent memory bloat
        if self._is_binary_garbage(raw_lines):
            log.warning(f"Agent {agent_id} ({agent.name}) output contains binary garbage — skipping capture")
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

        # Truncate excessively long lines to prevent memory bloat
        max_line_len = 5000
        new_lines = [
            line[:max_line_len] + " [truncated]" if len(line) > max_line_len else line
            for line in new_lines
        ]

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
        agent._total_lines_added += len(new_lines)

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
            if not agent._budget_warned:
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

    async def get_agent_cpu(self, agent_id: str) -> float:
        """Get CPU % of agent's process tree (sum of all children)."""
        agent = self.agents.get(agent_id)
        if not agent or not agent.pid:
            return 0.0

        try:
            proc = psutil.Process(agent.pid)
            total = proc.cpu_percent(interval=None)
            for child in proc.children(recursive=True):
                try:
                    total += child.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return round(total, 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    def check_system_pressure(self) -> dict[str, bool]:
        """Check if system is under resource pressure. Returns dict of pressure flags."""
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return {
                "cpu_pressure": cpu >= self.config.system_cpu_pressure_threshold,
                "memory_pressure": mem >= self.config.system_memory_pressure_threshold,
                "cpu_pct": cpu,
                "memory_pct": mem,
            }
        except Exception:
            return {"cpu_pressure": False, "memory_pressure": False, "cpu_pct": 0.0, "memory_pct": 0.0}

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
                wf_run.stage_started_at[agent.id] = time.monotonic()
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
            except Exception as e:
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

    def check_stage_timeouts(self) -> list[tuple[WorkflowRun, str, str]]:
        """Check all running workflow stages for timeouts. Returns list of (wf_run, agent_id, agent_name)."""
        timed_out: list[tuple[WorkflowRun, str, str]] = []
        now = time.monotonic()
        for wf_run in self.workflow_runs.values():
            if wf_run.status != "running":
                continue
            for agent_id in list(wf_run.running_ids):
                started = wf_run.stage_started_at.get(agent_id)
                if started and (now - started) > wf_run.stage_timeout_sec:
                    agent = self.agents.get(agent_id)
                    agent_name = agent.name if agent else agent_id
                    timed_out.append((wf_run, agent_id, agent_name))
        return timed_out

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
                # Track files touched count (O(1) via per-agent set)
                agent._files_touched_set.add(file_path)
                agent.files_touched = len(agent._files_touched_set)

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
                # Track files touched count (O(1) via per-agent set)
                agent._files_touched_set.add(file_path)
                agent.files_touched = len(agent._files_touched_set)

        return conflicts

    def _cleanup_file_activity(self, agent_id: str) -> None:
        """Remove an agent from all file activity tracking."""
        for file_path in list(self.file_activity.keys()):
            self.file_activity[file_path].pop(agent_id, None)
            if not self.file_activity[file_path]:
                del self.file_activity[file_path]

    def cleanup_all(self) -> None:
        """Kill all ashlr tmux sessions and clean temp files. Synchronous for shutdown."""
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
        """Kill any ashlr-* tmux sessions left from previous ungraceful shutdowns. Returns count killed."""
        killed = 0
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for session_name in result.stdout.strip().split("\n"):
                    # Clean up current prefix and legacy "ashlar-" sessions
                    if session_name.startswith(self.tmux_prefix + "-") or session_name.startswith("ashlar-"):
                        try:
                            subprocess.run(
                                ["tmux", "kill-session", "-t", session_name],
                                capture_output=True, timeout=5
                            )
                            killed += 1
                        except Exception as e:
                            log.warning(f"Failed to kill orphaned tmux session {session_name}: {e}")
        except Exception as e:
            log.warning(f"Failed to list tmux sessions for orphan cleanup: {e}")
        return killed


# ─────────────────────────────────────────────
# Section 5: Status Parser
# ─────────────────────────────────────────────


# Status detection moved to ashlr_ao.status — re-exported above



# Intelligence classes moved to ashlr_ao.intelligence — re-exported above
# [block removed — see ashlr_ao/intelligence.py]


# Database class moved to ashlr_ao.database — re-exported above



# ─────────────────────────────────────────────
# Section 5c: Rate Limiter
# ─────────────────────────────────────────────



# Middleware moved to ashlr_ao.middleware — re-exported above



# Metrics + WebSocket moved to ashlr_ao.websocket — re-exported above



# ─────────────────────────────────────────────
# Section 8: REST API Handlers
# ─────────────────────────────────────────────

# ── Auth Middleware ──

# _effective_max_agents and _check_feature moved to ashlr_ao.licensing — re-exported above


def _check_agent_ownership(request: web.Request, agent) -> web.Response | None:
    """Check if current user can control this agent. Returns error response or None if allowed."""
    user = request.get("user")
    if not user:
        return None  # No auth — allow (require_auth is false)
    # Admin can control any agent
    if user.role == "admin":
        return None
    # Owner can control their own agent
    if agent.owner_id and agent.owner_id != user.id:
        return web.json_response(
            {"error": "Only the agent owner or an admin can perform this action"}, status=403
        )
    return None


_AUTH_PUBLIC_ROUTES = frozenset({
    "/", "/logo.png", "/api/health",
    "/api/auth/login", "/api/auth/register", "/api/auth/verify", "/api/auth/status",
    "/api/license/status",
})


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    """Session-based auth middleware with bearer token fallback."""
    config: Config = request.app["config"]
    if not config.require_auth:
        return await handler(request)

    path = request.path
    if path in _AUTH_PUBLIC_ROUTES:
        return await handler(request)

    # WebSocket: validate session from cookie (sent automatically on same-origin)
    if path == "/ws":
        session_id = _extract_session_cookie(request)
        if session_id:
            db: Database = request.app["db"]
            sess = await db.get_session(session_id)
            if sess:
                user = await db.get_user_by_id(sess["user_id"])
                if user:
                    request["user"] = user
                    return await handler(request)
        # Fallback: bearer token for backward compat
        token = request.query.get("token", "")
        if token and config.auth_token and hmac.compare_digest(token, config.auth_token):
            return await handler(request)
        return web.json_response({"error": "Unauthorized"}, status=401)

    # Session cookie check
    session_id = _extract_session_cookie(request)
    if session_id:
        db: Database = request.app["db"]
        sess = await db.get_session(session_id)
        if sess:
            user = await db.get_user_by_id(sess["user_id"])
            if user:
                request["user"] = user
                return await handler(request)

    # Bearer token fallback (API/CLI access)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if config.auth_token and hmac.compare_digest(token, config.auth_token):
            return await handler(request)

    return web.json_response({"error": "Unauthorized"}, status=401)


def _extract_session_cookie(request: web.Request) -> str:
    """Extract ashlr_session cookie value from request."""
    cookie = request.cookies.get("ashlr_session", "")
    return cookie if cookie and len(cookie) >= 32 else ""


def _make_slug(name: str) -> str:
    """Convert org name to URL-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')
    return slug or "org"


def _set_session_cookie(response: web.Response, session_id: str, request: web.Request | None = None) -> None:
    """Set session cookie with appropriate security flags."""
    cookie_opts = {
        "httponly": True,
        "samesite": "Strict",
        "max_age": 86400,
        "path": "/",
    }
    # Use Secure flag if behind HTTPS reverse proxy
    if request and request.headers.get("X-Forwarded-Proto") == "https":
        cookie_opts["secure"] = True
    response.set_cookie("ashlr_session", session_id, **cookie_opts)


async def auth_status(request: web.Request) -> web.Response:
    """GET /api/auth/status — check if auth is required and if user is logged in."""
    config: Config = request.app["config"]
    db: Database = request.app["db"]

    if not config.require_auth:
        return web.json_response({"auth_required": False})

    user_count = await db.user_count()
    result = {"auth_required": True, "needs_setup": user_count == 0}

    # Check if current request has valid session
    session_id = _extract_session_cookie(request)
    if session_id:
        sess = await db.get_session(session_id)
        if sess:
            user = await db.get_user_by_id(sess["user_id"])
            if user:
                result["user"] = user.to_dict()

    return web.json_response(result)


async def auth_register(request: web.Request) -> web.Response:
    """POST /api/auth/register — register a new user. First user becomes admin + creates org."""
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip()
    org_name = str(data.get("org_name", "")).strip()

    if not email or "@" not in email:
        return web.json_response({"error": "Valid email required"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Password must be at least 8 characters"}, status=400)
    if not display_name:
        return web.json_response({"error": "Display name required"}, status=400)

    # Check if email already taken
    existing = await db.get_user_by_email(email)
    if existing:
        log.warning(f"Rejected registration: duplicate email {email} from {_get_client_ip(request)}")
        return web.json_response({"error": "Email already registered"}, status=409)

    user_count = await db.user_count()
    pw_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())

    if user_count == 0:
        # First user: create org + admin
        if not org_name:
            org_name = "My Team"
        org = await db.create_org(org_name, _make_slug(org_name))
        user = await db.create_user(email, display_name, pw_hash, role="admin", org_id=org.id)
        log.info(f"First user registered: {email} (admin) in org '{org_name}'")
    else:
        # Subsequent users must be invited (have a pre-created account with temp password)
        # For now, allow open registration if auth is enabled but check for invite
        return web.json_response({"error": "Registration requires an invite from an admin"}, status=403)

    # Auto-login after registration
    session_id = await db.create_session(user.id)
    await db.update_user_login(user.id)

    resp = web.json_response({"user": user.to_dict(), "org": org.to_dict()})
    _set_session_cookie(resp, session_id, request)
    return resp


async def auth_login(request: web.Request) -> web.Response:
    """POST /api/auth/login — authenticate with email/password."""
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    password = str(data.get("password", ""))

    if not email or not password:
        return web.json_response({"error": "Email and password required"}, status=400)

    ip = _get_client_ip(request)
    user = await db.get_user_by_email(email)
    if not user:
        log.warning(f"Failed login: unknown email {email} from {ip}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    if not await asyncio.to_thread(bcrypt.checkpw, password.encode(), user.password_hash.encode()):
        log.warning(f"Failed login: wrong password for {email} from {ip}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    session_id = await db.create_session(user.id)
    await db.update_user_login(user.id)
    log.info(f"Login: {email} from {ip}")

    org = await db.get_org(user.org_id) if user.org_id else None

    resp = web.json_response({
        "user": user.to_dict(),
        "org": org.to_dict() if org else None,
    })
    _set_session_cookie(resp, session_id, request)
    return resp


async def auth_logout(request: web.Request) -> web.Response:
    """POST /api/auth/logout — clear session."""
    db: Database = request.app["db"]
    session_id = _extract_session_cookie(request)
    if session_id:
        await db.delete_session(session_id)

    resp = web.json_response({"ok": True})
    resp.del_cookie("ashlr_session", path="/")
    return resp


async def auth_me(request: web.Request) -> web.Response:
    """GET /api/auth/me — return current user info from session."""
    db: Database = request.app["db"]

    session_id = _extract_session_cookie(request)
    if not session_id:
        return web.json_response({"error": "Not authenticated"}, status=401)

    sess = await db.get_session(session_id)
    if not sess:
        return web.json_response({"error": "Session expired"}, status=401)

    user = await db.get_user_by_id(sess["user_id"])
    if not user:
        return web.json_response({"error": "User not found"}, status=401)

    org = await db.get_org(user.org_id) if user.org_id else None
    return web.json_response({
        "user": user.to_dict(),
        "org": org.to_dict() if org else None,
    })


async def auth_invite(request: web.Request) -> web.Response:
    """POST /api/auth/invite — admin-only: create a new user with temp password."""
    if r := _check_feature(request, "multi_user"):
        return r
    db: Database = request.app["db"]

    # Must be authenticated as admin
    user = request.get("user")
    if not user or user.role != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    display_name = str(data.get("display_name", "")).strip()

    if not email or "@" not in email:
        return web.json_response({"error": "Valid email required"}, status=400)
    if not display_name:
        display_name = email.split("@")[0]

    existing = await db.get_user_by_email(email)
    if existing:
        return web.json_response({"error": "Email already registered"}, status=409)

    # Generate temp password
    temp_password = uuid.uuid4().hex[:12]
    pw_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode())

    invited_user = await db.create_user(email, display_name, pw_hash, role="member", org_id=user.org_id)
    log.info(f"User invited: {email} by {user.email}")

    return web.json_response({
        "user": invited_user.to_dict(),
        "temp_password": temp_password,
    })


async def auth_team(request: web.Request) -> web.Response:
    """GET /api/auth/team — list all users in the current user's org."""
    if r := _check_feature(request, "multi_user"):
        return r
    db: Database = request.app["db"]

    user = request.get("user")
    if not user:
        return web.json_response({"error": "Not authenticated"}, status=401)

    users = await db.get_org_users(user.org_id)
    return web.json_response({
        "users": [u.to_dict() for u in users],
    })


async def verify_auth(request: web.Request) -> web.Response:
    """POST /api/auth/verify — validate an auth token (backward compat)."""
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

    valid = bool(token and config.auth_token and hmac.compare_digest(token, config.auth_token))
    return web.json_response({"valid": valid, "auth_required": True})


async def serve_dashboard(request: web.Request) -> web.FileResponse:
    dashboard_path = Path(__file__).parent / "dashboard.html"
    if not dashboard_path.exists():
        return web.Response(text="Dashboard not found.", status=404)
    return web.FileResponse(dashboard_path)


async def serve_logo(request: web.Request) -> web.Response:
    logo_path = Path(__file__).parent / "logo.png"
    if not logo_path.exists():
        return web.Response(text="Logo not found", status=404)
    return web.FileResponse(logo_path, headers={"Cache-Control": "public, max-age=86400"})


async def list_agents(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agents_list = list(manager.agents.values())

    # Optional query filters
    branch_filter = request.query.get("branch")
    project_filter = request.query.get("project_id")
    status_filter = request.query.get("status")

    if branch_filter:
        agents_list = [a for a in agents_list if a.git_branch == branch_filter]
    if project_filter:
        agents_list = [a for a in agents_list if a.project_id == project_filter]
    if status_filter:
        agents_list = [a for a in agents_list if a.status == status_filter]

    return web.json_response([a.to_dict() for a in agents_list])


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

        # Set owner from authenticated user
        user = request.get("user")
        if user:
            agent.owner_id = user.id
            agent.owner_name = user.display_name

        # Broadcast to WebSocket clients
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        await hub.broadcast_event("agent_spawned", f"Agent {agent.name} spawned", agent.id, agent.name)

        return web.json_response(agent.to_dict(), status=201)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


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
    if err := _check_agent_ownership(request, agent):
        return err

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
    if err := _check_agent_ownership(request, agent):
        return err

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
    agent_name = agent.name if agent else agent_id
    return web.json_response({"error": f"Failed to send message to '{agent_name}' — agent may have terminated"}, status=500)


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
    db: Database = request.app["db"]
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
    color = data.get("color", "accent")
    if not isinstance(line_idx, int) or not isinstance(text, str):
        return web.json_response({"error": "Invalid bookmark data"}, status=400)
    if len(agent.bookmarks) >= 100:
        return web.json_response({"error": "Maximum 100 bookmarks per agent"}, status=400)
    bm_id = uuid.uuid4().hex[:6]
    bookmark = {
        "id": bm_id,
        "line": line_idx,
        "text": text[:200],
        "label": label[:100] if label else "",
        "color": color,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    agent.bookmarks.append(bookmark)
    # Persist to SQLite
    try:
        await db.add_bookmark(agent_id, line_idx, text[:200], label[:100], color)
    except Exception as e:
        log.debug(f"Failed to persist bookmark to DB: {e}")
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
    """GET /api/agents/{id}/bookmarks — list agent bookmarks (memory + SQLite)."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    # If in-memory is empty, try to load from SQLite
    if not agent.bookmarks:
        try:
            saved = await db.get_bookmarks(agent_id)
            for bm in saved:
                agent.bookmarks.append({
                    "id": str(bm.get("id", "")),
                    "line": bm.get("line_index", 0),
                    "text": bm.get("line_text", ""),
                    "label": bm.get("annotation", ""),
                    "color": bm.get("color", "accent"),
                    "created_at": bm.get("created_at", ""),
                })
        except Exception as e:
            log.error(f"Failed to load bookmarks: {e}")
    return web.json_response({"bookmarks": agent.bookmarks})


async def pause_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

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
    if err := _check_agent_ownership(request, agent):
        return err

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
    if err := _check_agent_ownership(request, agent):
        return err

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
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)
        except Exception:
            pass  # Non-JSON content type — proceed without task override

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
        for fop in list(a._file_operations)[-100:]:
            path = fop.file_path
            all_files[path] = all_files.get(path, 0) + 1
    top_files = sorted(all_files.items(), key=lambda x: -x[1])[:20]

    # Tool usage across all agents
    tool_counts: dict[str, int] = {}
    for a in agents:
        for inv in list(a._tool_invocations)[-200:]:
            tool_counts[inv.tool] = tool_counts.get(inv.tool, 0) + 1

    # Average health score
    health_scores = [a.health_score for a in agents if a.health_score > 0]
    avg_health = sum(health_scores) / len(health_scores) if health_scores else 0

    # Agent lifespans
    lifespans = []
    for a in agents:
        if a.created_at:
            try:
                created = datetime.fromisoformat(a.created_at.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                lifespans.append(age_min)
            except Exception as e:
                log.debug(f"Failed to parse agent lifespan for {a.id}: {e}")
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


async def collaboration_graph(request: web.Request) -> web.Response:
    """GET /api/collaboration — graph of agent relationships for visualization."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agents = list(manager.agents.values())

    nodes = []
    for a in agents:
        nodes.append({
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "status": a.status,
            "health_score": a.health_score,
            "project_id": a.project_id,
            "file_count": len(a._file_operations),
            "tool_count": len(a._tool_invocations),
        })

    edges: list[dict] = []
    edge_set: set[tuple[str, str, str]] = set()

    # 1. Message-based edges
    if db and db._db:
        try:
            async with db._db.execute(
                "SELECT from_agent_id, to_agent_id, COUNT(*) as cnt FROM agent_messages GROUP BY from_agent_id, to_agent_id"
            ) as cur:
                async for row in cur:
                    from_id, to_id, cnt = row[0], row[1], row[2]
                    if from_id in {a.id for a in agents} and to_id in {a.id for a in agents}:
                        key = (from_id, to_id, "message")
                        if key not in edge_set:
                            edge_set.add(key)
                            edges.append({"from": from_id, "to": to_id, "type": "message", "weight": cnt})
        except Exception as e:
            log.error(f"Failed to query collaboration graph: {e}")

    # 2. Shared file edges (agents writing/reading the same files)
    file_agents: dict[str, set[str]] = {}
    for a in agents:
        for fop in list(a._file_operations)[-200:]:
            path = fop.file_path
            if path not in file_agents:
                file_agents[path] = set()
            file_agents[path].add(a.id)

    # Use dict for O(1) edge weight updates instead of scanning edges list
    edge_dict: dict[tuple, dict] = {}  # (from_id, to_id, type) → edge dict
    for path, agent_ids in file_agents.items():
        ids = list(agent_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                key = (min(ids[i], ids[j]), max(ids[i], ids[j]), "shared_file")
                if key not in edge_set:
                    edge_set.add(key)
                    edge = {"from": ids[i], "to": ids[j], "type": "shared_file", "weight": 1, "file": path.split("/")[-1]}
                    edges.append(edge)
                    edge_dict[key] = edge
                else:
                    edge_dict[key]["weight"] += 1

    # 3. Same-project edges
    project_agents: dict[str, list[str]] = {}
    for a in agents:
        if a.project_id:
            if a.project_id not in project_agents:
                project_agents[a.project_id] = []
            project_agents[a.project_id].append(a.id)

    for proj, agent_ids in project_agents.items():
        for i in range(len(agent_ids)):
            for j in range(i + 1, len(agent_ids)):
                key = (min(agent_ids[i], agent_ids[j]), max(agent_ids[i], agent_ids[j]), "project")
                if key not in edge_set:
                    edge_set.add(key)
                    edge = {"from": agent_ids[i], "to": agent_ids[j], "type": "project", "weight": 1}
                    edges.append(edge)
                    edge_dict[key] = edge

    # 4. Conflict edges (from file_activity — agents writing to same files)
    agent_id_set = {a.id for a in agents}
    for path, agent_ops in manager.file_activity.items():
        writers = [aid for aid, op in agent_ops.items() if op == "write" and aid in agent_id_set]
        for i in range(len(writers)):
            for j in range(i + 1, len(writers)):
                key = (min(writers[i], writers[j]), max(writers[i], writers[j]), "conflict")
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append({"from": writers[i], "to": writers[j], "type": "conflict", "weight": 1, "file": path.split("/")[-1]})

    return web.json_response({"nodes": nodes, "edges": edges})


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
        except Exception as e:
            log.debug(f"Failed to get DB file size: {e}")

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
            "max_concurrent": config.max_agents,
            "port": config.port,
            "cost_budget_usd": config.cost_budget_usd,
        },
    })


async def run_diagnostic(request: web.Request) -> web.Response:
    """POST /api/diagnostic — run server self-test and report capabilities."""
    results: dict[str, dict] = {}

    # 1. tmux check
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-V", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        results["tmux"] = {"ok": True, "version": stdout.decode().strip()}
    except Exception as e:
        results["tmux"] = {"ok": False, "error": str(e)}

    # 2. Python version
    results["python"] = {"ok": True, "version": sys.version, "executable": sys.executable}

    # 3. Disk space
    try:
        disk = psutil.disk_usage("/")
        free_gb = disk.free / (1024**3)
        results["disk"] = {"ok": free_gb > 1.0, "free_gb": round(free_gb, 1), "pct_used": disk.percent}
    except Exception as e:
        results["disk"] = {"ok": False, "error": str(e)}

    # 4. Database write/read test
    try:
        db: Database = request.app["db"]
        if db and db._db:
            await db._db.execute("CREATE TABLE IF NOT EXISTS _diagnostic_test (id INTEGER PRIMARY KEY, val TEXT)")
            await db._db.execute("INSERT OR REPLACE INTO _diagnostic_test (id, val) VALUES (1, 'ok')")
            async with db._db.execute("SELECT val FROM _diagnostic_test WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            await db._db.execute("DROP TABLE IF EXISTS _diagnostic_test")
            await db._db.commit()
            results["database"] = {"ok": row and row[0] == "ok", "write_read": "pass"}
        else:
            results["database"] = {"ok": False, "error": "Database not available"}
    except Exception as e:
        results["database"] = {"ok": False, "error": str(e)}

    # 5. System resources
    try:
        mem = psutil.virtual_memory()
        results["system"] = {
            "ok": True,
            "cpu_count": psutil.cpu_count(),
            "cpu_pct": psutil.cpu_percent(interval=None),
            "memory_total_gb": round(mem.total / (1024**3), 1),
            "memory_available_gb": round(mem.available / (1024**3), 1),
            "memory_pct": mem.percent,
        }
    except Exception as e:
        results["system"] = {"ok": False, "error": str(e)}

    # 6. Backend availability
    manager: AgentManager = request.app["agent_manager"]
    backends = {}
    for name, bc in manager.backend_configs.items():
        backends[name] = {"available": bc.available, "command": bc.command}
    results["backends"] = {"ok": any(bc.available for bc in manager.backend_configs.values()), "backends": backends}

    all_ok = all(r.get("ok", False) for r in results.values())
    return web.json_response({
        "status": "ok" if all_ok else "degraded",
        "results": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def get_server_stats(request: web.Request) -> web.Response:
    """GET /api/stats — server uptime, request counts, agent lifecycle stats."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]

    uptime_sec = time.monotonic() - manager._start_time
    uptime_human = f"{int(uptime_sec // 3600)}h {int((uptime_sec % 3600) // 60)}m {int(uptime_sec % 60)}s"

    # Agent stats
    active = len(manager.agents)
    by_status: dict[str, int] = {}
    for agent in list(manager.agents.values()):
        by_status[agent.status] = by_status.get(agent.status, 0) + 1

    # DB size
    db_size_mb = 0.0
    try:
        if db.db_path.exists():
            db_size_mb = round(db.db_path.stat().st_size / (1024 * 1024), 2)
    except Exception as e:
        log.debug(f"Failed to get DB file size for costs: {e}")

    # Request count from logging middleware (tracked on manager)
    request_count = manager._total_api_requests

    return web.json_response({
        "uptime_sec": round(uptime_sec, 1),
        "uptime_human": uptime_human,
        "agents": {
            "active": active,
            "by_status": by_status,
            "total_spawned": manager._total_spawned,
            "total_killed": manager._total_killed,
            "total_messages_sent": manager._total_messages_sent,
        },
        "database": {
            "size_mb": db_size_mb,
            "path": str(db.db_path),
        },
        "config": {
            "max_agents": config.max_agents,
            "demo_mode": config.demo_mode,
            "llm_enabled": config.llm_enabled,
            "archive_max_rows_per_agent": config.archive_max_rows_per_agent,
            "archive_retention_hours": config.archive_retention_hours,
        },
        "request_count": request_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def validate_spawn(request: web.Request) -> web.Response:
    """POST /api/agents/validate — dry-run spawn validation.

    Validates all spawn parameters without actually creating an agent.
    Returns {valid: true, resolved: {...}} or {valid: false, errors: [...]}.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"valid": False, "errors": ["Invalid JSON body"]}, status=400)

    errors: list[str] = []
    warnings: list[str] = []
    resolved: dict[str, object] = {}
    manager: AgentManager = request.app["agent_manager"]
    config: Config = request.app["config"]

    # ── Role ──
    role = body.get("role", "general")
    if role not in BUILTIN_ROLES:
        errors.append(f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}")
    else:
        resolved["role"] = role
        resolved["role_name"] = BUILTIN_ROLES[role].name

    # ── Name ──
    name = body.get("name")
    if name:
        sanitized = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
        if not sanitized:
            errors.append("Agent name is empty after sanitization")
        else:
            resolved["name"] = sanitized
            existing_names = {a.name for a in list(manager.agents.values())}
            if sanitized in existing_names:
                warnings.append(f"Name '{sanitized}' already in use — will be suffixed with agent ID")
    else:
        resolved["name"] = "(auto-generated)"

    # ── Backend ──
    backend = body.get("backend", "claude-code")
    if not config.demo_mode:
        if backend not in config.backends:
            errors.append(f"Unknown backend '{backend}'. Available: {', '.join(config.backends.keys())}")
        else:
            bc = manager.backend_configs.get(backend)
            if bc and not bc.available:
                warnings.append(f"Backend '{backend}' is configured but CLI not found on PATH")
            resolved["backend"] = backend
    else:
        resolved["backend"] = backend

    # ── Working directory ──
    working_dir = body.get("working_dir")
    if working_dir:
        wd = os.path.abspath(os.path.expanduser(working_dir))
        if not os.path.isdir(wd):
            home_dir = str(Path.home())
            if not wd.startswith(home_dir):
                errors.append(f"Working directory does not exist and is outside home: {wd}")
            else:
                warnings.append(f"Working directory '{wd}' does not exist — will be created")
        resolved["working_dir"] = wd
    else:
        resolved["working_dir"] = os.path.abspath(os.path.expanduser(config.default_working_dir))

    # ── Task ──
    task = body.get("task", "")
    if task and len(task) > 10000:
        errors.append("Task description exceeds 10000 character limit")
    elif not task:
        warnings.append("No task provided — agent will start with no instructions")
    resolved["task_length"] = len(task)

    # ── Capacity ──
    agent_count = len(manager.agents)
    resolved["current_agents"] = agent_count
    resolved["max_agents"] = config.max_agents
    if agent_count >= config.max_agents:
        errors.append(f"Maximum agents ({config.max_agents}) already reached")

    # ── System pressure ──
    if config.spawn_pressure_block:
        pressure = manager.check_system_pressure()
        if pressure.get("cpu_pressure") or pressure.get("memory_pressure"):
            reasons = []
            if pressure.get("cpu_pressure"):
                reasons.append(f"CPU at {pressure.get('cpu_pct', 0):.0f}%")
            if pressure.get("memory_pressure"):
                reasons.append(f"Memory at {pressure.get('memory_pct', 0):.0f}%")
            errors.append(f"System under pressure ({', '.join(reasons)}), spawning blocked")
        resolved["system_pressure"] = pressure

    # ── Plan mode ──
    plan_mode = body.get("plan_mode", False)
    resolved["plan_mode"] = bool(plan_mode)

    valid = len(errors) == 0
    result: dict[str, object] = {"valid": valid, "resolved": resolved}
    if errors:
        result["errors"] = errors
    if warnings:
        result["warnings"] = warnings
    return web.json_response(result, status=200 if valid else 400)


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

    filename = f"ashlr-{agent.name}-{agent_id}.log"
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
    """Update runtime config and save to ashlr.yaml."""
    config: Config = request.app["config"]

    # Admin-only when auth is enabled
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required to update config"}, status=403)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Clamp max_agents to license ceiling
    lic: License = request.app.get("license", COMMUNITY_LICENSE)
    lic_max = lic.max_agents if lic.is_pro else COMMUNITY_LICENSE.max_agents
    if "max_agents" in data and isinstance(data["max_agents"], int):
        data["max_agents"] = min(data["max_agents"], lic_max)

    # Validation rules
    validators = {
        "max_agents": lambda v: isinstance(v, int) and 1 <= v <= 100,
        "default_role": lambda v: isinstance(v, str) and v in BUILTIN_ROLES,
        "default_working_dir": lambda v: isinstance(v, str) and len(v) > 0 and os.path.isdir(os.path.realpath(os.path.expanduser(v))),
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
        "llm_meta_interval": lambda v: isinstance(v, (int, float)) and 5.0 <= v <= 300.0,
        "log_level": lambda v: isinstance(v, str) and v.upper() in {"DEBUG", "INFO", "WARNING", "ERROR"},
        "host": lambda v: isinstance(v, str) and len(v) > 0 and all(c.isalnum() or c in ".-:" for c in v),
    }

    # Clamp max_restarts to [1, 10] range
    if "max_restarts" in data and isinstance(data["max_restarts"], int):
        data["max_restarts"] = max(1, min(10, data["max_restarts"]))

    errors = []
    for key, value in data.items():
        if key in validators and not validators[key](value):
            errors.append(f"Invalid value for {key}: {value}")

    # Validate alert_patterns compile as regex
    if "alert_patterns" in data and isinstance(data["alert_patterns"], list):
        for i, ap in enumerate(data["alert_patterns"]):
            if isinstance(ap, dict) and "pattern" in ap:
                try:
                    re.compile(ap["pattern"])
                except re.error as e:
                    errors.append(f"Invalid regex in alert_patterns[{i}]: {e}")

    if errors:
        return web.json_response({"error": "; ".join(errors)}, status=400)

    allowed_keys = set(validators.keys())

    # Build YAML-safe update dict
    yaml_update = {}
    agents_keys = {"max_agents": "max_concurrent", "default_role": "default_role",
                   "default_working_dir": "default_working_dir",
                   "output_capture_interval": "output_capture_interval_sec",
                   "memory_limit_mb": "memory_limit_mb", "default_backend": "default_backend"}
    llm_keys = {"llm_enabled": "enabled", "llm_model": "model", "llm_summary_interval": "summary_interval_sec", "llm_meta_interval": "meta_interval_sec"}
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
    config_path = ASHLR_DIR / "ashlr.yaml"
    try:
        def _write_config():
            raw = DEFAULT_CONFIG.copy()
            if config_path.exists():
                with open(config_path) as f:
                    raw = deep_merge(raw, yaml.safe_load(f) or {})
            raw = deep_merge(raw, yaml_update)
            tmp_path = config_path.with_suffix(".yaml.tmp")
            with open(tmp_path, "w") as f:
                yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
            tmp_path.rename(config_path)
        await asyncio.to_thread(_write_config)
        log.info(f"Config saved to disk: {', '.join(data.keys())}")
    except Exception as e:
        log.warning(f"Failed to save config to disk: {e}")
        try:
            config_path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
        except Exception as e2:
            log.debug(f"Failed to clean up temp config file: {e2}")
        # Do NOT update in-memory config — disk write failed
        return web.json_response({"error": f"Failed to save: {e}", "config": config.to_dict()}, status=500)

    # THEN: update in-memory config (disk write succeeded)
    for key in allowed_keys:
        if key in data and hasattr(config, key):
            setattr(config, key, data[key])

    # Update intelligence client config reference
    client = request.app.get("intelligence")
    if client:
        client.config = config

    # Recompile alert patterns if config changed
    if "alert_patterns" in data and isinstance(data["alert_patterns"], list):
        compiled_alerts = []
        for ap in data["alert_patterns"]:
            try:
                compiled_alerts.append((re.compile(ap["pattern"]), ap.get("severity", "warning"), ap.get("label", "Alert")))
            except (re.error, KeyError):
                pass
        request.app["_compiled_alert_patterns"] = compiled_alerts if compiled_alerts else None

    return web.json_response(config.to_dict())


# ── Project endpoints ──

async def list_projects(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    projects = await db.get_projects()
    # Enrich with agent counts and cost
    for proj in projects:
        agents = [a for a in list(manager.agents.values()) if a.project_id == proj["id"]]
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

    name = data.get("name")
    path = data.get("path")
    if not name or not isinstance(name, str) or not path or not isinstance(path, str):
        return web.json_response({"error": "name and path are required strings"}, status=400)

    resolved_path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved_path):
        return web.json_response({"error": f"Path is not a valid directory: {resolved_path}"}, status=400)
    home = str(Path.home())
    if not (resolved_path.startswith(home) or resolved_path.startswith("/tmp") or resolved_path.startswith("/private/tmp")):
        return web.json_response({"error": "Project path must be under home directory or /tmp"}, status=400)

    project = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "path": resolved_path,
        "description": str(data.get("description", "")),
    }
    try:
        await db.save_project(project)
    except Exception as e:
        log.error("Failed to save project: %s", e)
        return web.json_response({"error": "Failed to save project"}, status=500)
    return web.json_response(project, status=201)


async def delete_project(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    success = await db.delete_project(project_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Project not found"}, status=404)


async def update_project(request: web.Request) -> web.Response:
    """PUT /api/projects/{id} — update project name, path, or description."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    project_id = request.match_info["id"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict) or not data:
        return web.json_response({"error": "Request body must be a non-empty JSON object"}, status=400)

    # Validate path if provided
    if "path" in data:
        path = data["path"]
        if not isinstance(path, str) or not path:
            return web.json_response({"error": "path must be a non-empty string"}, status=400)
        resolved_path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(resolved_path):
            return web.json_response({"error": f"Path is not a valid directory: {resolved_path}"}, status=400)
        home = str(Path.home())
        if not (resolved_path.startswith(home) or resolved_path.startswith("/tmp") or resolved_path.startswith("/private/tmp")):
            return web.json_response({"error": "Project path must be under home directory or /tmp"}, status=400)
        data["path"] = resolved_path

    # Validate name if provided
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "name must be a non-empty string"}, status=400)
        data["name"] = name.strip()

    updated = await db.update_project(project_id, data)
    if not updated:
        return web.json_response({"error": "Project not found"}, status=404)

    # Broadcast update to all connected clients
    await hub.broadcast({"type": "project_updated", "project": updated})

    return web.json_response(updated)


# ── Workflow endpoints ──

async def list_workflows(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflows = await db.get_workflows()
    return web.json_response(workflows)


def _validate_workflow_specs(agent_specs: list) -> str | None:
    """Validate workflow agent specs for structure, deps, and circular deps. Returns error string or None."""
    if not isinstance(agent_specs, list) or len(agent_specs) == 0:
        return "agents must be a non-empty list"

    valid_indices = set(range(len(agent_specs)))
    for i, spec in enumerate(agent_specs):
        if not isinstance(spec, dict):
            return f"agents[{i}] must be an object"
        if not spec.get("role"):
            return f"agents[{i}] missing required field 'role'"
        deps = spec.get("depends_on")
        if deps:
            if not isinstance(deps, list):
                return f"agents[{i}].depends_on must be a list"
            for dep in deps:
                if not isinstance(dep, int) or dep not in valid_indices:
                    return f"agents[{i}].depends_on contains invalid index {dep} (valid: 0-{len(agent_specs)-1})"
                if dep == i:
                    return f"agents[{i}].depends_on cannot reference itself"

    # Circular dependency check via topological sort (DFS)
    WHITE, GRAY, BLACK = 0, 1, 2
    colors = [WHITE] * len(agent_specs)

    def has_cycle(node: int) -> bool:
        colors[node] = GRAY
        for dep in (agent_specs[node].get("depends_on") or []):
            if isinstance(dep, int) and 0 <= dep < len(agent_specs):
                if colors[dep] == GRAY:
                    return True
                if colors[dep] == WHITE and has_cycle(dep):
                    return True
        colors[node] = BLACK
        return False

    for i in range(len(agent_specs)):
        if colors[i] == WHITE and has_cycle(i):
            return f"Circular dependency detected involving agents[{i}]"

    return None


async def create_workflow(request: web.Request) -> web.Response:
    if r := _check_feature(request, "workflows"):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    wf_name = data.get("name")
    if not wf_name or not isinstance(wf_name, str) or not data.get("agents"):
        return web.json_response({"error": "name (string) and agents are required"}, status=400)

    agent_specs = data["agents"]
    error = _validate_workflow_specs(agent_specs)
    if error:
        return web.json_response({"error": error}, status=400)

    workflow = {
        "id": uuid.uuid4().hex[:8],
        "name": wf_name,
        "description": str(data.get("description", "")),
        "agents_json": agent_specs,
    }
    try:
        await db.save_workflow(workflow)
    except Exception as e:
        log.error("Failed to save workflow: %s", e)
        return web.json_response({"error": "Failed to save workflow"}, status=500)
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

    # Feature gate: workflows require Pro
    if r := _check_feature(request, "workflows"):
        return r

    # Capacity pre-check
    config: Config = request.app["config"]
    max_agents = _effective_max_agents(request.app)
    agents_needed = len(workflow.get("agents", []))
    current_count = len(manager.agents)
    if current_count + agents_needed > max_agents:
        return web.json_response({
            "error": f"Not enough capacity: need {agents_needed} agents, "
                     f"but only {max_agents - current_count} slots available "
                     f"({current_count}/{max_agents} in use)",
        }, status=503)

    agent_specs = workflow.get("agents", [])
    has_deps = any(spec.get("depends_on") for spec in agent_specs)

    if has_deps:
        # Check for circular dependencies before starting
        cycles = WorkflowRun.detect_circular_deps(agent_specs)
        if cycles:
            cycle_desc = "; ".join(f"[{' -> '.join(str(c) for c in cycle)}]" for cycle in cycles[:3])
            return web.json_response({
                "error": f"Circular dependency detected in workflow: {cycle_desc}",
                "cycles": cycles[:3],
            }, status=400)

        # DAG pipeline mode — create WorkflowRun and start with root agents
        run_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        # Per-stage timeout from request or workflow config (default 30 min)
        stage_timeout = body.get("stage_timeout_sec", workflow.get("stage_timeout_sec", 1800.0))
        try:
            stage_timeout = max(60.0, min(float(stage_timeout), 7200.0))  # Clamp 1min-2hr
        except (TypeError, ValueError):
            stage_timeout = 1800.0

        wf_run = WorkflowRun(
            id=run_id,
            workflow_id=workflow_id,
            workflow_name=workflow["name"],
            agent_specs=agent_specs,
            pending_indices=set(range(len(agent_specs))),
            working_dir=working_dir or config.default_working_dir,
            created_at=now,
            stage_timeout_sec=stage_timeout,
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
    if r := _check_feature(request, "workflows"):
        return r
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

    # Full validation including circular dep check
    error = _validate_workflow_specs(agents)
    if error:
        return web.json_response({"error": error}, status=400)

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

    if r := _check_agent_ownership(request, from_agent):
        return r

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_agent_id = data.get("to_agent_id")
    content = data.get("content", "")
    if not to_agent_id or not content:
        return web.json_response({"error": "to_agent_id and content required"}, status=400)
    if len(content) > 50000:
        return web.json_response({"error": "content too long (max 50,000 chars)"}, status=400)

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

    messages = await db.get_messages_for_agent(agent_id, limit, offset)
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

    if err := _check_agent_ownership(request, from_agent):
        return err

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

    key_findings = str(data.get("key_findings", ""))[:5000]
    files_modified = data.get("files_modified", [])
    if not isinstance(files_modified, list):
        return web.json_response({"error": "files_modified must be a list"}, status=400)
    files_modified = [str(f)[:500] for f in files_modified[:50]]

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
        "invocations": [t.to_dict() for t in list(agent._tool_invocations)[-limit:]],
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
        "operations": [f.to_dict() for f in list(agent._file_operations)[-limit:]],
        "total": len(agent._file_operations),
    })


async def search_agent_output(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/output/search?q=pattern&regex=false&context=2&limit=100 — search agent output."""
    if r := _check_rate(request, cost=2):
        return r
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    query = request.query.get("q", "").strip()
    if not query or len(query) > 500:
        return web.json_response({"error": "Query 'q' required (max 500 chars)"}, status=400)

    use_regex = request.query.get("regex", "false").lower() in ("true", "1")
    try:
        context_lines = max(0, min(int(request.query.get("context", "2")), 10))
    except ValueError:
        context_lines = 2
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100

    # Compile pattern
    try:
        if use_regex:
            pattern = re.compile(query, re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error as e:
        return web.json_response({"error": f"Invalid regex: {e}"}, status=400)

    lines = list(agent.output_lines)
    matches = []
    for i, line in enumerate(lines):
        stripped = _strip_ansi(line) if callable(_strip_ansi) else line
        if pattern.search(stripped):
            # Gather context
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            ctx = [_strip_ansi(lines[j]) if callable(_strip_ansi) else lines[j] for j in range(start, end)]
            matches.append({
                "line_index": i + agent._archived_lines,
                "line": stripped[:500],
                "context": ctx,
            })
            if len(matches) >= limit:
                break

    return web.json_response({
        "agent_id": agent_id,
        "query": query,
        "regex": use_regex,
        "matches": matches,
        "total_matches": len(matches),
        "total_lines": len(lines) + agent._archived_lines,
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
    if err := _check_agent_ownership(request, agent):
        return err
    snap = agent.create_snapshot(trigger="manual")
    return web.json_response(snap.to_dict(), status=201)


async def get_intelligence_insights(request: web.Request) -> web.Response:
    """GET /api/intelligence/insights — current cross-agent insights."""
    if r := _check_feature(request, "intelligence"):
        return r
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
    if r := _check_feature(request, "intelligence"):
        return r
    client: IntelligenceClient | None = request.app.get("intelligence")
    manager: AgentManager = request.app["agent_manager"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    transcript = data.get("transcript", "").strip()
    if not transcript:
        return web.json_response({"error": "Empty transcript"}, status=400)

    # If intelligence client is available, use it for NLU
    if client and client.available:
        try:
            intent = await client.parse_command(
                transcript,
                list(manager.agents.values()),
                {"total_agents": len(manager.agents)},
            )
            return web.json_response({
                "intent": intent.to_dict(),
                "source": "xai",
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
    client: IntelligenceClient | None = request.app.get("intelligence")
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)

    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if not client or not client.available:
        return web.json_response({"error": "LLM not configured"}, status=503)

    summary = await client.summarize(
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

async def get_resumable_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions/resumable — list recently completed agents that can be resumed."""
    db: Database = request.app["db"]
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
    except ValueError:
        limit = 20
    sessions = await db.get_resumable_sessions(limit)
    return web.json_response({"sessions": sessions})


async def resume_from_history(request: web.Request) -> web.Response:
    """POST /api/sessions/{id}/resume — re-spawn an agent from its archived session.

    Optionally override task or append instructions via 'task' and 'continue_message' fields.
    """
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    session_id = request.match_info["id"]

    # Look up the archived session
    session = await db.get_agent_history_item(session_id)
    if not session:
        return web.json_response({"error": "Session not found"}, status=404)

    # Parse optional override fields
    try:
        data = await request.json()
    except Exception:
        data = {}

    task = data.get("task", session.get("task", ""))
    continue_msg = data.get("continue_message", "")
    if continue_msg:
        task = f"{task}\n\nContinuation: {continue_msg}" if task else continue_msg
    plan_mode = data.get("plan_mode", bool(session.get("plan_mode", 0)))

    # Verify working directory still exists
    working_dir = session.get("working_dir", os.getcwd())
    if not os.path.isdir(working_dir):
        return web.json_response({"error": f"Working directory no longer exists: {working_dir}"}, status=400)

    # Check agent limit (license-aware)
    config: Config = request.app["config"]
    effective_max = _effective_max_agents(request.app)
    if len(manager.agents) >= effective_max:
        return web.json_response({"error": f"Agent limit reached ({effective_max})"}, status=409)

    # Spawn a new agent with the archived session's config
    backend = session.get("backend", config.default_backend)
    model = data.get("model") or session.get("model") or None
    tools_raw = data.get("tools") or session.get("tools_allowed") or None
    tools = tools_raw if isinstance(tools_raw, list) else None

    try:
        new_agent = await manager.spawn(
            name=session.get("name", "resumed"),
            role=session.get("role", "general"),
            task=task,
            working_dir=working_dir,
            backend=backend,
            plan_mode=plan_mode,
            model=model,
            tools=tools,
            resume_session=session_id,
        )
        if session.get("project_id"):
            new_agent.project_id = session["project_id"]
    except Exception as e:
        return web.json_response({"error": f"Failed to resume: {e}"}, status=500)

    # Broadcast the new agent
    await hub.broadcast({
        "type": "agent_update",
        "agent": new_agent.to_dict(),
    })
    await hub.broadcast_event("agent_spawned", f"Resumed {new_agent.name} from session {session_id[:8]}", new_agent.id)

    return web.json_response({
        "agent": new_agent.to_dict(),
        "resumed_from": session_id,
    }, status=201)


async def export_fleet_state(request: web.Request) -> web.Response:
    """GET /api/fleet/export — export current fleet state as JSON for backup/restore."""
    manager: AgentManager = request.app["agent_manager"]
    config: Config = request.app["config"]
    db: Database = request.app["db"]

    agents_data = []
    for agent in list(manager.agents.values()):
        agents_data.append({
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "status": agent.status,
            "task": agent.task,
            "summary": agent.summary,
            "working_dir": agent.working_dir,
            "backend": agent.backend,
            "project_id": agent.project_id,
            "plan_mode": agent.plan_mode,
            "context_pct": agent.context_pct,
            "tokens_input": agent.tokens_input,
            "tokens_output": agent.tokens_output,
            "estimated_cost_usd": agent.estimated_cost_usd,
            "created_at": agent.created_at,
            "health_score": agent.health_score,
            "error_count": agent.error_count,
            "restart_count": agent.restart_count,
        })

    projects = await db.get_projects() if db else []

    export_data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agents": agents_data,
        "agents_count": len(agents_data),
        "projects": projects,
        "config_summary": {
            "max_agents": config.max_agents,
            "default_backend": config.default_backend,
            "fleet_cost_limit": config.cost_budget_usd,
            "intelligence_enabled": config.llm_enabled,
        },
    }
    return web.json_response(export_data)


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

    if len(manager.agents) >= _effective_max_agents(request.app):
        return web.json_response({"error": "Max agents reached"}, status=409)

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
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Unknown role '{role}'"}, status=400)
    backend = data.get("backend")
    if backend:
        config_temp: Config = request.app["config"]
        valid_backends = set(KNOWN_BACKENDS.keys()) | set(config_temp.backends.keys())
        if backend not in valid_backends:
            return web.json_response({"error": f"Unknown backend '{backend}'"}, status=400)
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


# ── Bookmark endpoints ──


async def get_bookmarks(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/bookmarks — list bookmarks for an agent."""
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    bookmarks = await db.get_bookmarks(agent_id)
    return web.json_response(bookmarks)


async def add_bookmark(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/bookmarks — add a bookmark to agent output."""
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    line_index = data.get("line_index")
    if line_index is None or not isinstance(line_index, int):
        return web.json_response({"error": "line_index (int) required"}, status=400)
    line_text = data.get("line_text", "")
    annotation = data.get("annotation", "")
    color = data.get("color", "accent")
    bm_id = await db.add_bookmark(agent_id, line_index, str(line_text), str(annotation), str(color))
    return web.json_response({"id": bm_id, "agent_id": agent_id, "line_index": line_index}, status=201)


async def delete_bookmark(request: web.Request) -> web.Response:
    """DELETE /api/bookmarks/{id} — delete a bookmark."""
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    try:
        bm_id = int(request.match_info["id"])
    except (ValueError, KeyError):
        return web.json_response({"error": "Invalid bookmark ID"}, status=400)
    success = await db.delete_bookmark(bm_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Bookmark not found"}, status=404)


# ── Config Import/Export endpoints ──

async def export_config(request: web.Request) -> web.Response:
    """GET /api/config/export — download full ashlr.yaml as JSON."""
    if r := _check_rate(request, cost=1):
        return r
    config_path = ASHLR_DIR / "ashlr.yaml"
    if not config_path.exists():
        return web.json_response({"error": "Config file not found"}, status=404)
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return web.json_response(raw, headers={
            "Content-Disposition": "attachment; filename=ashlr_config.json"
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

    # Allowlist safe config sections — never allow backends, server, or auth_token
    _SAFE_IMPORT_KEYS = {"display", "agents", "llm", "voice", "licensing"}
    unsafe_keys = set(data.keys()) - _SAFE_IMPORT_KEYS
    if unsafe_keys:
        return web.json_response(
            {"error": f"Import not allowed for sections: {', '.join(sorted(unsafe_keys))}. "
             f"Allowed: {', '.join(sorted(_SAFE_IMPORT_KEYS))}"},
            status=400,
        )
    # Strip dangerous nested fields even within allowed sections
    if isinstance(data.get("agents"), dict):
        data["agents"].pop("backends", None)

    # Read current config for diff
    config_path = ASHLR_DIR / "ashlr.yaml"
    current = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                current = yaml.safe_load(f) or {}
        except Exception as e:
            log.debug(f"Failed to load current config for diff preview: {e}")

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

    # Strip security-sensitive fields from import payload
    if isinstance(data.get("server"), dict):
        data["server"].pop("auth_token", None)
        data["server"].pop("host", None)

    # Atomic write (validated)
    try:
        tmp_path = config_path.with_suffix(".yaml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.rename(config_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
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
        except Exception as e:
            log.error(f"Failed to get project paths from DB: {e}")
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

    pattern = None
    if use_regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(query, flags)
        except re.error:
            return web.json_response({"error": "invalid regex"}, status=400)

    # Snapshot search data to avoid holding references during thread execution
    search_data = []
    for agent in list(manager.agents.values()):
        if agent_filter and agent.id != agent_filter:
            continue
        search_data.append((agent.id, agent.name, agent.role, list(agent.output_lines or [])))

    def _do_search() -> list[dict]:
        results: list[dict] = []
        query_lower = query.lower() if not case_sensitive else ""
        for agent_id, agent_name, agent_role, lines in search_data:
            for i, line in enumerate(lines):
                if len(results) >= max_results:
                    return results
                matched = False
                if pattern:
                    matched = bool(pattern.search(line))
                elif case_sensitive:
                    matched = query in line
                else:
                    matched = query_lower in line.lower()
                if matched:
                    ctx_before = lines[i - 1] if i > 0 else ""
                    ctx_after = lines[i + 1] if i + 1 < len(lines) else ""
                    results.append({
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "agent_role": agent_role,
                        "line_index": i,
                        "line": line[:500],
                        "context_before": ctx_before[:500],
                        "context_after": ctx_after[:500],
                    })
            if len(results) >= max_results:
                break
        return results

    results = await asyncio.to_thread(_do_search)

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

    if err := _check_agent_ownership(request, agent):
        return err

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

    # Feature gate: fleet_presets requires Pro
    if r := _check_feature(request, "fleet_presets"):
        return r

    # Check concurrent agent limit before spawning
    current_count = len(manager.agents)
    max_agents = _effective_max_agents(request.app)
    available_slots = max_agents - current_count
    if available_slots <= 0:
        suffix = " Upgrade to Pro for more." if not request.app.get("license", COMMUNITY_LICENSE).is_pro else ""
        return web.json_response({"error": f"Agent limit reached ({max_agents}).{suffix}"}, status=409)
    if len(agent_specs) > available_slots:
        return web.json_response(
            {"error": f"Only {available_slots} agent slots available, but {len(agent_specs)} requested"},
            status=409,
        )

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
            await manager.send_message(aid, message)
            success_ids.append(aid)
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        except Exception as e:
            failed_items.append({"id": aid, "error": str(e)})

    return web.json_response({"success": success_ids, "failed": failed_items})

# Background tasks moved to ashlr_ao.background — re-exported above



# ─────────────────────────────────────────────
# License API Endpoints
# ─────────────────────────────────────────────

async def license_status(request: web.Request) -> web.Response:
    """GET /api/license/status — current license info (public)."""
    lic: License = request.app.get("license", COMMUNITY_LICENSE)
    gated = {}
    for feat in sorted(PRO_FEATURES):
        gated[feat] = lic.is_pro  # True if unlocked
    return web.json_response({
        "license": lic.to_dict(),
        "effective_max_agents": _effective_max_agents(request.app),
        "gated_features": gated,
    })


async def activate_license(request: web.Request) -> web.Response:
    """POST /api/license/activate — activate a license key (admin-only when auth enabled)."""
    config: Config = request.app["config"]
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = str(data.get("license_key", "")).strip()
    if not key:
        return web.json_response({"error": "license_key is required"}, status=400)

    lic = validate_license(key)
    if not lic.is_pro:
        return web.json_response({"error": "Invalid or expired license key"}, status=400)

    # Persist to DB (org) if auth is enabled
    db: Database = request.app["db"]
    if config.require_auth:
        user = request.get("user")
        if user and user.org_id:
            await db.update_org_license(user.org_id, key, lic.tier)

    # Persist to config YAML
    try:
        config_path = ASHLR_DIR / "ashlr.yaml"
        if config_path.exists():
            with open(config_path) as f:
                raw_yaml = yaml.safe_load(f) or {}
        else:
            raw_yaml = {}
        raw_yaml.setdefault("licensing", {})["key"] = key
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".yaml.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
            Path(tmp_path).rename(config_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except Exception as e:
        log.warning(f"Failed to persist license key to config: {e}")

    # Update runtime state
    request.app["license"] = lic
    manager: AgentManager = request.app["agent_manager"]
    manager.license = lic
    config.license_key = key
    log.info(f"License activated: tier={lic.tier}, max_agents={lic.max_agents}, expires={lic.expires_at}")

    # Broadcast to all WS clients
    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({"type": "license_update", "license": lic.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})

    return web.json_response({"license": lic.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})


async def deactivate_license(request: web.Request) -> web.Response:
    """DELETE /api/license/deactivate — remove license and revert to Community."""
    config: Config = request.app["config"]
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required"}, status=403)

    # Clear from DB
    db: Database = request.app["db"]
    if config.require_auth:
        user = request.get("user")
        if user and user.org_id:
            await db.update_org_license(user.org_id, "", "community")

    # Clear from config YAML
    try:
        config_path = ASHLR_DIR / "ashlr.yaml"
        if config_path.exists():
            with open(config_path) as f:
                raw_yaml = yaml.safe_load(f) or {}
            raw_yaml.setdefault("licensing", {})["key"] = ""
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".yaml.tmp")
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
                Path(tmp_path).rename(config_path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
    except Exception as e:
        log.warning(f"Failed to clear license key from config: {e}")

    # Revert runtime state
    request.app["license"] = COMMUNITY_LICENSE
    manager: AgentManager = request.app["agent_manager"]
    manager.license = COMMUNITY_LICENSE
    config.license_key = ""
    log.info("License deactivated — reverted to Community")

    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({"type": "license_update", "license": COMMUNITY_LICENSE.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})

    return web.json_response({"license": COMMUNITY_LICENSE.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})


def create_app(config: Config) -> web.Application:
    middlewares = [security_headers_middleware, request_logging_middleware, compression_middleware, rate_limit_middleware]
    if config.require_auth:
        middlewares.append(auth_middleware)
    app = web.Application(middlewares=middlewares, client_max_size=10 * 1024 * 1024)
    app["config"] = config

    manager = AgentManager(config)
    app["agent_manager"] = manager
    app["license"] = COMMUNITY_LICENSE

    db = Database()
    app["db"] = db
    app["db_available"] = True  # Set to False if DB init fails in start_background_tasks
    manager.db = db  # Give manager DB ref for cleanup operations (e.g., release file locks on kill)

    hub = WebSocketHub(manager, config, db)
    hub.app = app
    app["ws_hub"] = hub

    # Rate limiter
    rate_limiter = RateLimiter()
    app["rate_limiter"] = rate_limiter

    # Unified Intelligence Client (xAI Grok)
    intel_client = IntelligenceClient(config)
    app["intelligence"] = intel_client
    if intel_client.available:
        log.info(f"Intelligence enabled: xai/{config.llm_model}")
    else:
        log.info("Intelligence disabled (set XAI_API_KEY and llm.enabled in config)")

    # Compile alert patterns for pattern alerting in capture loop
    compiled_alerts = []
    for ap in config.alert_patterns:
        try:
            compiled_alerts.append((re.compile(ap["pattern"]), ap.get("severity", "warning"), ap.get("label", "Alert")))
        except (re.error, KeyError) as e:
            log.warning(f"Skipping invalid alert pattern: {ap} — {e}")
    app["_compiled_alert_patterns"] = compiled_alerts if compiled_alerts else None
    if compiled_alerts:
        log.info(f"Pattern alerting enabled: {len(compiled_alerts)} patterns")
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
    app.router.add_post("/api/agents/validate", validate_spawn)
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
    app.router.add_get("/api/agents/{id}/output/search", search_agent_output)
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
    app.router.add_get("/api/collaboration", collaboration_graph)

    # REST API — System
    app.router.add_get("/api/health", health_check)
    app.router.add_get("/api/health/detailed", health_detailed)
    app.router.add_post("/api/diagnostic", run_diagnostic)
    app.router.add_get("/api/stats", get_server_stats)
    app.router.add_get("/api/system", system_metrics)
    app.router.add_get("/api/roles", list_roles)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/backends", list_backends)
    app.router.add_post("/api/auth/verify", verify_auth)
    app.router.add_get("/api/auth/status", auth_status)
    app.router.add_post("/api/auth/register", auth_register)
    app.router.add_post("/api/auth/login", auth_login)
    app.router.add_post("/api/auth/logout", auth_logout)
    app.router.add_get("/api/auth/me", auth_me)
    app.router.add_post("/api/auth/invite", auth_invite)
    app.router.add_get("/api/auth/team", auth_team)
    app.router.add_get("/api/costs", get_costs)
    app.router.add_get("/api/conflicts", list_conflicts)

    # REST API — Projects
    app.router.add_get("/api/projects", list_projects)
    app.router.add_post("/api/projects", create_project)
    app.router.add_delete("/api/projects/{id}", delete_project)
    app.router.add_put("/api/projects/{id}", update_project)

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

    # REST API — Session Resume
    app.router.add_get("/api/sessions/resumable", get_resumable_sessions)
    app.router.add_post("/api/sessions/{id}/resume", resume_from_history)

    # REST API — Fleet Export
    app.router.add_get("/api/fleet/export", export_fleet_state)

    # REST API — Task Queue
    app.router.add_get("/api/queue", list_queue)
    app.router.add_post("/api/queue", add_to_queue)
    app.router.add_delete("/api/queue/{id}", remove_from_queue)

    # REST API — Scratchpad
    app.router.add_get("/api/scratchpad", get_scratchpad)
    app.router.add_put("/api/scratchpad", upsert_scratchpad)
    app.router.add_delete("/api/scratchpad/{key}", delete_scratchpad_entry)

    # Note: bookmark routes already registered at lines above (add_agent_bookmark, delete_agent_bookmark, list_agent_bookmarks)

    # REST API — Config Import/Export
    app.router.add_get("/api/config/export", export_config)
    app.router.add_post("/api/config/import", import_config)

    # REST API — Extensions
    app.router.add_get("/api/extensions", get_extensions)
    app.router.add_post("/api/extensions/refresh", refresh_extensions)

    # REST API — Licensing
    app.router.add_get("/api/license/status", license_status)
    app.router.add_post("/api/license/activate", activate_license)
    app.router.add_delete("/api/license/deactivate", deactivate_license)

    # Signal handlers (register in on_startup when event loop is running)
    async def _register_signals(app: web.Application) -> None:
        setup_signal_handlers(app["agent_manager"])
    app.on_startup.append(_register_signals)

    # Background tasks
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app


def setup_signal_handlers(agent_manager: AgentManager) -> None:
    """Register async-safe signal handlers using loop.add_signal_handler."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop yet — use synchronous signal.signal as fallback
        def handle_shutdown(signum: int, frame: Any) -> None:
            print("\n\033[33m→ Shutting down Ashlr...\033[0m")
            agent_manager.cleanup_all()
            raise KeyboardInterrupt()
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        return

    _shutdown_requested = False

    def _signal_handler() -> None:
        nonlocal _shutdown_requested
        if _shutdown_requested:
            return
        _shutdown_requested = True
        print("\n\033[33m→ Shutting down Ashlr...\033[0m")
        asyncio.ensure_future(_async_shutdown(agent_manager))

    loop.add_signal_handler(signal.SIGINT, _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)


async def _async_shutdown(agent_manager: AgentManager) -> None:
    """Async-safe shutdown: runs cleanup then raises GracefulExit."""
    try:
        await asyncio.to_thread(agent_manager.cleanup_all)
        print("\033[32m✓ All agent sessions cleaned up\033[0m")
    except Exception as e:
        log.warning(f"Shutdown cleanup error: {e}")
    raise web.GracefulExit()


def main() -> None:
    from ashlr_ao import __version__

    parser = argparse.ArgumentParser(
        prog="ashlr",
        description="Ashlr AO — Agent Orchestration Platform",
    )
    parser.add_argument("-V", "--version", action="version", version=f"ashlr {__version__}")
    parser.add_argument("-p", "--port", type=int, help="server port (default: 5111)")
    parser.add_argument("-H", "--host", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--demo", action="store_true", help="force demo mode")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="log level")
    args = parser.parse_args()

    print_banner()
    has_claude = check_dependencies()

    # --demo flag forces demo mode via env var (same mechanism as CLAUDECODE)
    if args.demo:
        os.environ["CLAUDECODE"] = "1"

    config = load_config(has_claude)

    # CLI args > env vars > config file
    if args.port is not None:
        config.port = args.port
    if args.host is not None:
        config.host = args.host
    if args.log_level is not None:
        config.log_level = args.log_level

    setup_logging(config.log_level)

    app = create_app(config)

    # Clean up orphaned tmux sessions from prior ungraceful shutdowns
    manager = app["agent_manager"]
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
            log.error(f"  → Set ASHLR_PORT=8080 to use a different port")
            log.error(f"  → On macOS, AirPlay Receiver may use port 5000 (Ashlr defaults to 5111 to avoid this)")
            log.error(f"    System Settings > General > AirDrop & Handoff > AirPlay Receiver → off")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
