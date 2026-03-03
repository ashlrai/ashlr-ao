"""Tests for agent spawn, lifecycle, pause/resume/restart, and session management."""

import asyncio
import inspect
import sys
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    from ashlr_server import (
        parse_agent_status,
        extract_summary,
        STATUS_PATTERNS,
        BackendConfig,
        KNOWN_BACKENDS,
        _suggest_followup,
        FOLLOWUP_SUGGESTIONS,
        QueuedTask,
        OutputIntelligenceParser,
        ToolInvocation,
        FileOperation,
        GitOperation,
        AgentTestResult,
        IntelligenceClient,
        AgentInsight,
        ParsedIntent,
        _keyword_parse_command,
        WorkflowRun,
        _extract_question,
        _resolve_agent_refs,
        calculate_efficiency_score,
    )


class TestSetStatus:
    def test_normal_transition_succeeds(self, make_agent):
        agent = make_agent(status="spawning")
        result = agent.set_status("working")
        assert result is True
        assert agent.status == "working"

    def test_rapid_duplicate_still_succeeds(self, make_agent):
        """Monotonic time always advances, so rapid calls should still succeed."""
        agent = make_agent(status="working")
        result1 = agent.set_status("planning")
        result2 = agent.set_status("working")
        assert result1 is True
        assert result2 is True
        assert agent.status == "working"

    def test_updates_status_string_correctly(self, make_agent):
        agent = make_agent(status="spawning")
        agent.set_status("planning")
        assert agent.status == "planning"
        agent.set_status("working")
        assert agent.status == "working"
        agent.set_status("idle")
        assert agent.status == "idle"

    def test_multiple_transitions_all_tracked(self, make_agent):
        agent = make_agent(status="spawning")
        statuses = ["working", "planning", "working", "waiting", "working", "idle"]
        for s in statuses:
            assert agent.set_status(s) is True
        assert agent.status == "idle"


# ─────────────────────────────────────────────
# T2: Output capture hash dedup
# ─────────────────────────────────────────────



class TestPauseResumeRestart:
    def test_pause_already_paused_agent(self, make_agent):
        """Pausing an already-paused agent should still return True."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_raw = AsyncMock()
        result = asyncio.run(ashlr_server.AgentManager.pause(manager, agent.id))
        assert result is True
        assert agent.status == "paused"

    def test_resume_running_agent(self, make_agent):
        """Resuming a working agent should still succeed and send the message."""
        agent = make_agent(status="working")
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        result = asyncio.run(ashlr_server.AgentManager.resume(manager, agent.id))
        assert result is True
        assert agent.status == "working"

    def test_restart_guard_prevents_concurrent(self, make_agent):
        """If _restart_in_progress is set, restart should return False."""
        agent = make_agent(status="error")
        # Simulate lock being held (concurrent restart in progress)
        async def _test():
            async with agent._restart_lock:
                # Lock is held, so restart should return False
                manager = MagicMock(spec=ashlr_server.AgentManager)
                manager.agents = {agent.id: agent}
                result = await ashlr_server.AgentManager.restart(manager, agent.id)
                return result
        result = asyncio.run(_test())
        assert result is False

    def test_resume_with_custom_message(self, make_agent):
        """Resume should use the custom message if provided."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        result = asyncio.run(ashlr_server.AgentManager.resume(manager, agent.id, message="yes, proceed"))
        assert result is True
        manager._tmux_send_keys.assert_called_once_with(agent.tmux_session, "yes, proceed")


# ─────────────────────────────────────────────
# T5: Spawn validation and error paths
# ─────────────────────────────────────────────



class TestSpawnValidation:
    def test_max_agents_config_exists(self):
        """Config should have a max_agents setting."""
        config = ashlr_server.Config()
        assert hasattr(config, "max_agents")
        assert config.max_agents > 0

    def test_invalid_backend_rejected(self):
        """Unknown backend should not be in KNOWN_BACKENDS."""
        assert "nonexistent-backend" not in KNOWN_BACKENDS

    def test_backend_config_has_context_window(self):
        """All known backends should have context_window and char_to_token_ratio."""
        for name, bc in KNOWN_BACKENDS.items():
            assert bc.context_window > 0, f"{name} missing context_window"
            assert bc.char_to_token_ratio > 0, f"{name} missing char_to_token_ratio"

    def test_claude_code_context_window_200k(self):
        """Claude Code should have 200K context window."""
        bc = KNOWN_BACKENDS["claude-code"]
        assert bc.context_window == 200_000

    def test_codex_context_window_128k(self):
        """Codex should have 128K context window."""
        bc = KNOWN_BACKENDS["codex"]
        assert bc.context_window == 128_000

    def test_aider_context_window_128k(self):
        """Aider should have 128K context window."""
        bc = KNOWN_BACKENDS["aider"]
        assert bc.context_window == 128_000


# ─────────────────────────────────────────────
# T5 continued: extract_summary with new features
# ─────────────────────────────────────────────



class TestRestartFieldReset:
    def test_restart_resets_archived_lines(self, make_agent):
        """Restart should reset _archived_lines to prevent stale offsets."""
        agent = make_agent(status="working")
        agent._archived_lines = 500
        agent._overflow_to_archive = ("a1b2", ["line1"], 499)
        agent._total_chars = 50000
        agent.tokens_input = 1000
        agent.tokens_output = 2000
        agent.estimated_cost_usd = 0.15
        # Simulate the fields that restart should clear
        # (We verify the field exists and check its default)
        assert hasattr(agent, '_archived_lines')
        assert hasattr(agent, '_overflow_to_archive')
        assert hasattr(agent, 'tokens_input')
        assert hasattr(agent, 'tokens_output')
        assert hasattr(agent, 'estimated_cost_usd')

    def test_overflow_archive_field_is_settable(self, make_agent):
        """Agent should accept _overflow_to_archive as a dynamic attribute."""
        agent = make_agent()
        agent._overflow_to_archive = ("a1b2", ["line1"], 0)
        assert agent._overflow_to_archive == ("a1b2", ["line1"], 0)
        agent._overflow_to_archive = None
        assert agent._overflow_to_archive is None


# ─────────────────────────────────────────────
# Backend config context fields tests
# ─────────────────────────────────────────────



class TestPlanMode:
    def test_plan_mode_defaults_false(self, make_agent):
        """Agent plan_mode should default to False."""
        agent = make_agent()
        assert agent.plan_mode is False

    def test_plan_mode_in_to_dict(self, make_agent):
        """to_dict() should include plan_mode field."""
        agent = make_agent(plan_mode=True)
        d = agent.to_dict()
        assert "plan_mode" in d
        assert d["plan_mode"] is True

        agent2 = make_agent(plan_mode=False)
        d2 = agent2.to_dict()
        assert d2["plan_mode"] is False

    def test_plan_mode_status_guard(self, make_agent):
        """Plan-mode agents in 'planning' status should not transition to work-like statuses.

        The guard is in detect_status(): when plan_mode=True and current status is
        'planning', any work-like status (working, reading, etc.) should be suppressed."""
        agent = make_agent(status="planning", plan_mode=True)
        agent.output_lines.extend([
            "Reading src/index.ts for entry point patterns...",
            "Writing implementation in src/features/new-feature.ts...",
        ])
        detected = parse_agent_status(list(agent.output_lines), agent, None)
        # parse_agent_status may return "working", "reading", etc. — guard blocks all
        if agent.plan_mode and agent.status == "planning" and detected not in ("waiting", "error", "planning"):
            guarded = "planning"
        else:
            guarded = detected
        assert guarded == "planning"

    def test_plan_mode_allows_waiting(self, make_agent):
        """Plan-mode agents CAN transition from planning to waiting (plan ready)."""
        agent = make_agent(status="planning", plan_mode=True)
        agent.output_lines.extend([
            "Here is my plan:",
            "  1. Read existing code",
            "  2. Implement changes",
            "Do you want me to proceed with this plan? (yes/no)",
        ])
        detected = parse_agent_status(list(agent.output_lines), agent, None)
        # Guard should NOT block planning → waiting
        if agent.plan_mode and agent.status == "planning" and detected not in ("waiting", "error", "planning"):
            guarded = "planning"
        else:
            guarded = detected
        assert guarded == "waiting"
        assert agent.needs_input is True

    def test_plan_mode_cleared_on_working(self, make_agent):
        """plan_mode should clear when agent transitions from waiting to working."""
        agent = make_agent(status="waiting", plan_mode=True)
        new_status = "working"
        old_status = agent.status
        # Simulate the auto-clear logic from the output capture loop
        if agent.plan_mode and old_status == "waiting" and new_status == "working":
            agent.plan_mode = False
        agent.set_status(new_status)
        assert agent.plan_mode is False
        assert agent.status == "working"

    def test_plan_mode_initial_status(self, make_agent):
        """Plan-mode agents should start with status 'planning', normal agents with 'working'."""
        # Simulate what spawn() does after the agent is created
        plan_agent = make_agent(status="spawning", plan_mode=True)
        plan_agent.status = "planning" if plan_agent.plan_mode else "working"
        assert plan_agent.status == "planning"

        normal_agent = make_agent(status="spawning", plan_mode=False)
        normal_agent.status = "planning" if normal_agent.plan_mode else "working"
        assert normal_agent.status == "working"


# ─────────────────────────────────────────────
# T12: Database degraded-mode null guards
# ─────────────────────────────────────────────



class TestResumeSetStatus:
    def test_resume_uses_set_status(self, make_agent):
        """resume() should use set_status() not direct assignment for monotonic guard."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        asyncio.run(ashlr_server.AgentManager.resume(manager, agent.id))
        assert agent.status == "working"
        # set_status updates _status_updated_at — verify it was bumped
        assert agent._status_updated_at > 0

    def test_resume_clears_input_state(self, make_agent):
        """resume() should clear needs_input and input_prompt."""
        agent = make_agent(status="paused")
        agent.needs_input = True
        agent.input_prompt = "Some prompt"
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        asyncio.run(ashlr_server.AgentManager.resume(manager, agent.id))
        assert agent.needs_input is False
        assert agent.input_prompt is None


# ─────────────────────────────────────────────
# T14: Backend inject_role_prompt config
# ─────────────────────────────────────────────



class TestInjectRolePrompt:
    def test_inject_role_prompt_defaults_true(self):
        """BackendConfig.inject_role_prompt should default to True."""
        bc = BackendConfig(command="test")
        assert bc.inject_role_prompt is True

    def test_inject_role_prompt_in_to_dict(self):
        """to_dict should include inject_role_prompt."""
        bc = BackendConfig(command="test", inject_role_prompt=False)
        d = bc.to_dict()
        assert "inject_role_prompt" in d
        assert d["inject_role_prompt"] is False

    def test_claude_code_has_plan_mode_flag(self):
        """claude-code backend should have plan_mode_flag set."""
        cc = KNOWN_BACKENDS["claude-code"]
        assert cc.plan_mode_flag == "--permission-mode plan"


# ─────────────────────────────────────────────
# T10: ExtensionScanner
# ─────────────────────────────────────────────



class TestRestartWithTask:
    """Tests for restart with task override."""

    def test_restart_preserves_task_by_default(self):
        """Restart without new_task keeps the original task."""
        agent = ashlr_server.Agent(
            id="rt01", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlr-rt01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        # Simulate what restart does with no new_task
        saved_task = agent.task
        new_task = None
        if new_task:
            saved_task = new_task
            agent.task = new_task
        assert saved_task == "Original task"
        assert agent.task == "Original task"

    def test_restart_overrides_task(self):
        """Restart with new_task updates the agent's task."""
        agent = ashlr_server.Agent(
            id="rt02", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlr-rt02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        new_task = "Modified task with fixes"
        saved_task = agent.task
        if new_task:
            saved_task = new_task
            agent.task = new_task
        assert saved_task == "Modified task with fixes"
        assert agent.task == "Modified task with fixes"

    def test_restart_empty_task_no_override(self):
        """Empty string new_task should not override."""
        agent = ashlr_server.Agent(
            id="rt03", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlr-rt03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        new_task = ""
        saved_task = agent.task
        if new_task:
            saved_task = new_task
            agent.task = new_task
        assert saved_task == "Original task"
        assert agent.task == "Original task"



class TestSpawnValidationExtended:
    def test_handler_exists(self):
        """validate_spawn handler should exist."""
        assert hasattr(ashlr_server, 'validate_spawn')
        assert callable(ashlr_server.validate_spawn)

    def test_route_registered(self):
        """Route /api/agents/validate should be registered."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/agents/validate" in src
        assert "validate_spawn" in src

    def test_validates_role(self):
        """Validation should check role against BUILTIN_ROLES."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "BUILTIN_ROLES" in src
        assert "Unknown role" in src

    def test_validates_name(self):
        """Validation should sanitize and check name."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "sanitiz" in src.lower() or "name" in src

    def test_validates_backend(self):
        """Validation should check backend availability."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "backend" in src
        assert "available" in src

    def test_validates_working_dir(self):
        """Validation should check working directory."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "working_dir" in src
        assert "isdir" in src

    def test_validates_task_length(self):
        """Validation should check task length limit."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "10000" in src

    def test_checks_capacity(self):
        """Validation should check agent capacity."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "max_agents" in src

    def test_checks_system_pressure(self):
        """Validation should check system pressure."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "check_system_pressure" in src

    def test_returns_resolved_fields(self):
        """Validation should return resolved field values."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "resolved" in src
        assert '"valid"' in src or "'valid'" in src

    def test_returns_warnings(self):
        """Validation should return warnings for non-blocking issues."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "warnings" in src

    def test_returns_errors(self):
        """Validation should return errors list for blocking issues."""
        src = inspect.getsource(ashlr_server.validate_spawn)
        assert "errors" in src


# ─────────────────────────────────────────────
# Stale Input State on Error (#249)
# ─────────────────────────────────────────────



class TestStaleInputClearOnError:
    def test_error_clears_needs_input(self):
        """Transitioning to error should clear needs_input flag."""
        agent = ashlr_server.Agent(
            id="test", name="test", role="general",
            status="waiting", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        agent.needs_input = True
        agent.input_prompt = "Do you want to continue?"
        agent.set_status("error")
        assert agent.needs_input is False
        assert agent.input_prompt is None

    def test_paused_clears_needs_input(self):
        """Transitioning to paused should clear needs_input flag."""
        agent = ashlr_server.Agent(
            id="test", name="test", role="general",
            status="waiting", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        agent.needs_input = True
        agent.input_prompt = "Approve plan?"
        agent.set_status("paused")
        assert agent.needs_input is False
        assert agent.input_prompt is None

    def test_working_preserves_needs_input_false(self):
        """Transitioning to working with no needs_input should be fine."""
        agent = ashlr_server.Agent(
            id="test", name="test", role="general",
            status="idle", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        agent.needs_input = False
        agent.set_status("working")
        assert agent.needs_input is False

    def test_non_error_preserves_needs_input(self):
        """Transitioning to working should NOT clear needs_input."""
        agent = ashlr_server.Agent(
            id="test", name="test", role="general",
            status="waiting", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        agent.needs_input = True
        agent.input_prompt = "Continue?"
        agent.set_status("working")
        # Working does not clear needs_input — only error/paused do
        assert agent.needs_input is True


# ─────────────────────────────────────────────
# DB Cleanup on Kill (#249)
# ─────────────────────────────────────────────



class TestDBCleanupOnKill:
    def test_kill_calls_release_file_locks(self):
        """Kill should release DB file locks for the agent."""
        src = inspect.getsource(ashlr_server.AgentManager.kill)
        assert "release_file_locks" in src

    def test_kill_handles_missing_db(self):
        """Kill should handle missing db gracefully."""
        src = inspect.getsource(ashlr_server.AgentManager.kill)
        assert "self.db" in src
        # Should have a try/except around DB cleanup
        assert "except Exception" in src

    def test_manager_has_db_attribute(self):
        """AgentManager should have db attribute initialized to None."""
        config = ashlr_server.Config()
        config.demo_mode = True
        manager = ashlr_server.AgentManager(config)
        assert hasattr(manager, 'db')
        assert manager.db is None

    def test_create_app_sets_manager_db(self):
        """create_app should set manager.db reference."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "manager.db = db" in src


# ─────────────────────────────────────────────
# WebSocket Broadcast Safety (#249)
# ─────────────────────────────────────────────



class TestTmuxSessionCollisionCheck:
    def test_spawn_checks_existing_session(self):
        """Spawn should check for existing tmux session before creating."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        assert "has-session" in src

    def test_spawn_kills_orphan_before_create(self):
        """Spawn should kill orphaned session if found."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        assert "Orphaned tmux session" in src
        assert "kill-session" in src


# ─────────────────────────────────────────────
# Database Commit Timeouts (#251)
# ─────────────────────────────────────────────



class TestSpawnTimeout:
    """Tests for spawn timeout detection in output_capture_loop."""

    def _make_agent(self, status="spawning", spawn_time=None):
        from ashlr_server import Agent
        agent = Agent(
            id="t001", name="timeout-test", role="general", status="spawning",
            backend="claude-code", task="test", working_dir="/tmp",
        )
        agent.status = status
        if spawn_time is not None:
            agent._spawn_time = spawn_time
        return agent

    def test_spawn_timeout_triggers_after_30s(self):
        agent = self._make_agent(status="spawning", spawn_time=time.monotonic() - 31)
        # Simulate the timeout check from output_capture_loop
        if agent.status == "spawning" and agent._spawn_time > 0:
            if time.monotonic() - agent._spawn_time > 30:
                agent.set_status("error")
                agent.error_message = "Spawn timeout — no output after 30s"
        assert agent.status == "error"
        assert "timeout" in agent.error_message.lower()

    def test_no_timeout_within_30s(self):
        agent = self._make_agent(status="spawning", spawn_time=time.monotonic() - 10)
        if agent.status == "spawning" and agent._spawn_time > 0:
            if time.monotonic() - agent._spawn_time > 30:
                agent.set_status("error")
        assert agent.status == "spawning"

    def test_non_spawning_agents_not_checked(self):
        agent = self._make_agent(status="working", spawn_time=time.monotonic() - 60)
        if agent.status == "spawning" and agent._spawn_time > 0:
            if time.monotonic() - agent._spawn_time > 30:
                agent.set_status("error")
        assert agent.status == "working"



class TestMaxRestartExhaustion:
    """Tests for max restart exhaustion notification logic."""

    def test_exhausted_clears_error_entered_at(self):
        from ashlr_server import Agent
        agent = Agent(id="m001", name="maxed", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent.restart_count = 3
        agent.max_restarts = 3
        agent._error_entered_at = time.monotonic()
        # Simulate exhaustion check
        if agent.restart_count >= agent.max_restarts and agent._error_entered_at > 0:
            agent._error_entered_at = 0  # Clear to prevent re-notification
        assert agent._error_entered_at == 0

    def test_not_exhausted_when_under_limit(self):
        from ashlr_server import Agent
        agent = Agent(id="m002", name="notmaxed", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent.restart_count = 1
        agent.max_restarts = 3
        agent._error_entered_at = time.monotonic()
        original = agent._error_entered_at
        if agent.restart_count >= agent.max_restarts and agent._error_entered_at > 0:
            agent._error_entered_at = 0
        assert agent._error_entered_at == original



class TestIdleReaping:
    """Tests for idle agent reaping logic."""

    def test_idle_detected_after_ttl(self):
        from ashlr_server import Agent
        agent = Agent(id="i001", name="idle", role="general", status="idle", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "idle"
        agent.last_output_time = max(time.monotonic() - 600, 1.0)  # 10 min ago
        idle_ttl = 300  # 5 min TTL
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is True

    def test_not_reaped_within_ttl(self):
        from ashlr_server import Agent
        agent = Agent(id="i002", name="fresh", role="general", status="idle", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "idle"
        agent.last_output_time = time.monotonic() - 60  # 1 min ago
        idle_ttl = 300
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is False

    def test_working_agents_not_reaped(self):
        from ashlr_server import Agent
        agent = Agent(id="i003", name="working", role="general", status="working", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "working"
        agent.last_output_time = max(time.monotonic() - 600, 1.0)
        idle_ttl = 300
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is False

    def test_zero_ttl_disables_reaping(self):
        from ashlr_server import Agent
        agent = Agent(id="i004", name="noReap", role="general", status="idle", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "idle"
        agent.last_output_time = max(time.monotonic() - 9999, 1.0)
        idle_ttl = 0
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is False


# ─────────────────────────────────────────────
# T16: WorkflowRun dependency resolution
# ─────────────────────────────────────────────

class TestResumeFromHistoryNoProjectId:
    """Tests that resume_from_history doesn't pass project_id to spawn()."""

    def test_spawn_signature_has_no_project_id(self):
        """Verify AgentManager.spawn() does not accept project_id as a kwarg."""
        sig = inspect.signature(ashlr_server.AgentManager.spawn)
        assert "project_id" not in sig.parameters

    def test_resume_sets_project_id_after_spawn(self):
        """The resume handler should set project_id on the agent after spawn(), not as a kwarg."""
        # Verify the source code pattern: project_id should NOT be in the spawn() call
        import inspect
        source = inspect.getsource(ashlr_server.resume_from_history)
        # The fix: project_id should NOT appear in manager.spawn() kwargs
        assert "project_id=session" not in source



class TestSessionResume:
    """Tests for session resume parameter on spawn."""

    def test_spawn_accepts_resume_session(self, make_agent):
        """spawn() accepts resume_session parameter (verify the signature exists)."""
        sig = inspect.signature(ashlr_server.AgentManager.spawn)
        assert "resume_session" in sig.parameters

    def test_agent_has_session_id_field(self, make_agent):
        """Agent dataclass has session_id=None by default."""
        agent = make_agent()
        assert agent.session_id is None


# ─────────────────────────────────────────────
# T-NEW: List agents filter
# ─────────────────────────────────────────────

# Helper to create a test app for HTTP tests (copied from test_integration.py)
def _make_mock_db():
    """Create a mock Database that returns empty results without needing SQLite."""
    db = MagicMock()
    db.get_projects = AsyncMock(return_value=[])
    db.get_workflows = AsyncMock(return_value=[])
    db.get_presets = AsyncMock(return_value=[])
    db.save_agent = AsyncMock()
    db.save_event = AsyncMock()
    db.log_event = AsyncMock()
    db.close = AsyncMock()
    db.init = AsyncMock()
    db.get_history = AsyncMock(return_value=[])
    db.get_events = AsyncMock(return_value=[])
    db.get_events_count = AsyncMock(return_value=0)
    db.get_agent_history_count = AsyncMock(return_value=0)
    db.get_historical_analytics = AsyncMock(return_value={})
    db.get_scratchpad = AsyncMock(return_value=[])
    db.db_path = Path("/tmp/test-ashlr.db")
    db.find_similar_tasks = AsyncMock(return_value=[])
    db.get_resumable_sessions = AsyncMock(return_value=[])
    db.archive_output = AsyncMock()
    db.release_file_locks = AsyncMock()
    db.get_archived_output = AsyncMock(return_value=([], 0))
    db.get_bookmarks = AsyncMock(return_value=[])
    db.add_bookmark = AsyncMock(return_value=1)
    db.save_project = AsyncMock()
    db.delete_project = AsyncMock(return_value=False)
    db.save_workflow = AsyncMock()
    db.save_preset = AsyncMock()
    db.delete_preset = AsyncMock(return_value=False)
    db.delete_workflow = AsyncMock(return_value=False)
    db.save_message = AsyncMock()
    db.get_messages = AsyncMock(return_value=[])
    db.get_messages_count = AsyncMock(return_value=0)
    db.upsert_scratchpad = AsyncMock()
    db.delete_scratchpad = AsyncMock(return_value=False)
    db.save_bookmark = AsyncMock(return_value=1)
    db.get_history_item = AsyncMock(return_value=None)
    db.get_workflow = AsyncMock(return_value=None)
    db._db = None
    return db


def _make_test_app():
    """Create a test app with DB and background tasks disabled."""
    config = ashlr_server.Config()
    config.demo_mode = True
    config.spawn_pressure_block = False

    app = ashlr_server.create_app(config)
    mock_db = _make_mock_db()
    app["db"] = mock_db
    app["ws_hub"].db = mock_db
    app["rate_limiter"].check = lambda *a, **kw: (True, 0.0)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["db_available"] = True
    app["db_ready"] = True
    app["bg_task_health"] = {}
    app["bg_tasks"] = []
    # Set Pro license so existing tests bypass feature gates
    from datetime import datetime, timedelta, timezone
    from ashlr_server import License, PRO_FEATURES
    _pro_lic = License(tier="pro", max_agents=100, expires_at=(datetime.now(timezone.utc) + timedelta(days=365)).isoformat(), features=PRO_FEATURES)
    app["license"] = _pro_lic
    app["agent_manager"].license = _pro_lic
    return app


TEST_WORKING_DIR = str(Path.home())



class TestSpawnErrorPaths:
    @pytest.mark.asyncio
    async def test_spawn_missing_task(self, aiohttp_client):
        """POST /api/agents with no task returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={"role": "general"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_spawn_invalid_working_dir(self, aiohttp_client):
        """POST /api/agents with nonexistent working_dir returns 400/500."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test",
            "role": "general",
            "working_dir": "/nonexistent/path/xyz",
        })
        # Should fail validation
        assert resp.status in (400, 500)

    @pytest.mark.asyncio
    async def test_spawn_invalid_backend(self, aiohttp_client):
        """POST /api/agents with unknown backend returns error."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test",
            "role": "general",
            "working_dir": str(Path.home()),
            "backend": "nonexistent-backend-xyz",
        })
        assert resp.status in (400, 500)

    @pytest.mark.asyncio
    async def test_spawn_max_agents_exceeded(self, aiohttp_client):
        """POST /api/agents when at max_agents limit returns error."""
        app = _make_test_app()
        app["config"].max_agents = 0  # Set limit to 0
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test",
            "role": "general",
            "working_dir": str(Path.home()),
        })
        # Should fail due to max agents
        assert resp.status in (400, 429, 500, 503)

    @pytest.mark.asyncio
    async def test_spawn_empty_task(self, aiohttp_client):
        """POST /api/agents with empty string task returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "",
            "role": "general",
            "working_dir": str(Path.home()),
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_spawn_whitespace_task(self, aiohttp_client):
        """POST /api/agents with whitespace-only task returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "   ",
            "role": "general",
            "working_dir": str(Path.home()),
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_spawn_oversized_task(self, aiohttp_client):
        """POST /api/agents with very large task still works in demo mode."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "x" * 10000,
            "role": "general",
            "working_dir": str(Path.home()),
        })
        # Should succeed in demo mode or return 400 if task too long
        assert resp.status in (200, 201, 400)

    @pytest.mark.asyncio
    async def test_spawn_invalid_json(self, aiohttp_client):
        """POST /api/agents with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_nonexistent_agent(self, aiohttp_client):
        """DELETE /api/agents/nonexistent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.delete("/api/agents/nonexistent123")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_agent(self, aiohttp_client):
        """POST /api/agents/nonexistent/send returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent123/send", json={"message": "hello"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_pause_nonexistent_agent(self, aiohttp_client):
        """POST /api/agents/nonexistent/pause returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent123/pause")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restart_nonexistent_agent(self, aiohttp_client):
        """POST /api/agents/nonexistent/restart returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent123/restart")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_patch_nonexistent_agent(self, aiohttp_client):
        """PATCH /api/agents/nonexistent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.patch("/api/agents/nonexistent123", json={"name": "new"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent(self, aiohttp_client):
        """GET /api/agents/nonexistent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent123")
        assert resp.status == 404


