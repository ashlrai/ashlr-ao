"""
Ashlr AO — Agent Manager

Core orchestration class: spawns, manages, and monitors AI coding agents
running in tmux sessions. Handles lifecycle, workflows, file conflicts,
context tracking, and system pressure response.
"""

from __future__ import annotations

import asyncio
import collections
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from ashlr_ao.backends import BackendConfig, KNOWN_BACKENDS
from ashlr_ao.config import Config
from ashlr_ao.constants import (
    ASHLR_DIR,
    _ANSI_ESCAPE_RE,
    _strip_ansi,
    log,
    redact_secrets,
)
from ashlr_ao.licensing import COMMUNITY_LICENSE, License
from ashlr_ao.models import (
    Agent,
    QueuedTask,
    WorkflowRun,
)
from ashlr_ao.roles import BUILTIN_ROLES
from ashlr_ao.status import parse_agent_status

if TYPE_CHECKING:
    from ashlr_ao.database import Database
    from ashlr_ao.websocket import WebSocketHub


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
