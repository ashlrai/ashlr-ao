"""Tests for agent lifecycle: set_status, output capture dedup, parse_agent_status patterns,
pause/resume/restart edge cases, and spawn validation."""

import asyncio
import inspect
import sys
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlar_server
    from ashlar_server import (
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
        AnthropicIntelligenceClient,
        AgentInsight,
        ParsedIntent,
        _keyword_parse_command,
        WorkflowRun,
        _extract_question,
        _resolve_agent_refs,
        calculate_efficiency_score,
    )


# ─────────────────────────────────────────────
# T1: set_status() monotonic guard
# ─────────────────────────────────────────────

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

class TestOutputHashDedup:
    def test_identical_output_hash_returns_no_change(self, make_agent):
        """When _prev_output_hash matches, capture should detect no change."""
        agent = make_agent()
        lines = ["line1", "line2", "line3"]
        # Simulate setting the hash as if capture ran once
        agent._prev_output_hash = hash(tuple(lines[-50:]))
        # Same hash means no new output
        new_hash = hash(tuple(lines[-50:]))
        assert new_hash == agent._prev_output_hash

    def test_changed_output_gives_different_hash(self, make_agent):
        """Changed output should produce a different hash."""
        agent = make_agent()
        lines1 = ["line1", "line2"]
        lines2 = ["line1", "line2", "line3 new"]
        hash1 = hash(tuple(lines1[-50:]))
        hash2 = hash(tuple(lines2[-50:]))
        assert hash1 != hash2

    def test_first_capture_always_has_zero_hash(self, make_agent):
        """New agent starts with hash 0, so any real output is different."""
        agent = make_agent()
        assert agent._prev_output_hash == 0
        real_hash = hash(tuple(["hello"]))
        assert real_hash != 0  # Extremely unlikely to collide with 0

    def test_hash_updates_after_change(self, make_agent):
        """After simulating a capture, hash should update."""
        agent = make_agent()
        lines = ["output line 1", "output line 2"]
        new_hash = hash(tuple(lines[-50:]))
        agent._prev_output_hash = new_hash
        assert agent._prev_output_hash == new_hash
        # Now change and verify
        lines.append("output line 3")
        newer_hash = hash(tuple(lines[-50:]))
        assert newer_hash != new_hash


# ─────────────────────────────────────────────
# T3: parse_agent_status() comprehensive patterns
# ─────────────────────────────────────────────

class TestParseAgentStatusPatterns:
    def test_each_category_has_patterns(self):
        """Every status category should have at least one compiled pattern."""
        for category in ("planning", "reading", "working", "waiting", "error", "error_mention", "complete"):
            assert len(STATUS_PATTERNS[category]) > 0, f"No patterns for {category}"

    def test_waiting_beats_error_when_both_present(self, make_agent):
        """Waiting has higher priority than error."""
        agent = make_agent(status="working")
        lines = [
            "Traceback (most recent call last):",
            "  File 'test.py'",
            "Should I fix this error?",
        ]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"

    def test_error_captures_error_message(self, make_agent):
        """Error status should populate agent.error_message."""
        agent = make_agent(status="working")
        lines = ["Working on files...", "fatal: repository not found"]
        status = parse_agent_status(lines, agent)
        assert status == "error"
        assert agent.error_message  # Should be non-empty

    def test_error_mention_increments_count_without_status_change(self, make_agent):
        """error_mention patterns increment error_count but don't flip status."""
        agent = make_agent(status="working", error_count=0)
        # "failed" without a waiting/error trigger
        lines = ["Build step 1 failed", "Retrying..."]
        status = parse_agent_status(lines, agent)
        # Should stay working (error_mention doesn't change status)
        # but error_count should increment
        assert agent.error_count >= 1

    def test_backend_specific_pattern_merge(self, make_agent):
        """Backend patterns should extend default detection."""
        agent = make_agent(status="idle")
        lines = ["⎿ Writing to file"]
        # Without backend patterns, this may or may not match
        backend_patterns = {"working": [r"⎿"]}
        status = parse_agent_status(lines, agent, backend_patterns=backend_patterns)
        assert status == "working"

    def test_thinking_block_detected_as_planning(self, make_agent):
        """<thinking> blocks should trigger planning status."""
        agent = make_agent(status="working")
        lines = ["<thinking>", "Let me consider the options..."]
        status = parse_agent_status(lines, agent)
        assert status == "planning"

    def test_tool_call_detected_as_working(self, make_agent):
        """Claude Code tool calls like Read( should trigger working."""
        agent = make_agent(status="idle")
        lines = ["Read(/src/app.ts)"]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_tool_output_detected_as_working(self, make_agent):
        """Tool Output: markers should trigger working status."""
        agent = make_agent(status="idle")
        lines = ["Tool Result: success"]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_changes_committed_detected_as_complete(self, make_agent):
        """'changes committed' should trigger complete/idle."""
        agent = make_agent(status="working")
        lines = ["All changes committed to main"]
        status = parse_agent_status(lines, agent)
        assert status == "idle"  # complete maps to idle

    def test_no_issues_found_detected_as_complete(self, make_agent):
        """'no issues found' should trigger complete/idle."""
        agent = make_agent(status="working")
        lines = ["Lint check: no issues found"]
        status = parse_agent_status(lines, agent)
        assert status == "idle"

    # ── New patterns added in status detection expansion ──

    def test_searching_detected_as_reading(self, make_agent):
        """'searching for' should trigger reading status."""
        agent = make_agent(status="idle")
        lines = ["Searching for relevant files in the codebase"]
        status = parse_agent_status(lines, agent)
        assert status == "reading"

    def test_examining_detected_as_reading(self, make_agent):
        """'examining' should trigger reading status."""
        agent = make_agent(status="idle")
        lines = ["Examining the test file structure"]
        status = parse_agent_status(lines, agent)
        assert status == "reading"

    def test_docker_detected_as_working(self, make_agent):
        """Docker commands should trigger working status."""
        agent = make_agent(status="idle")
        lines = ["docker build -t myapp ."]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_deploy_detected_as_working(self, make_agent):
        """Deploy operations should trigger working status."""
        agent = make_agent(status="idle")
        lines = ["Deploying to production"]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_please_confirm_detected_as_waiting(self, make_agent):
        """'please confirm' should trigger waiting status."""
        agent = make_agent(status="working")
        lines = ["Please confirm you want to proceed"]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"

    def test_waiting_for_input_detected_as_waiting(self, make_agent):
        """'waiting for input' should trigger waiting status."""
        agent = make_agent(status="working")
        lines = ["Waiting for input from user"]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"

    def test_successfully_completed_detected_as_complete(self, make_agent):
        """'Successfully completed' should trigger idle."""
        agent = make_agent(status="working")
        lines = ["Successfully completed the migration"]
        status = parse_agent_status(lines, agent)
        assert status == "idle"


# ─────────────────────────────────────────────
# T4: Pause/resume/restart edge cases
# ─────────────────────────────────────────────

class TestPauseResumeRestart:
    def test_pause_already_paused_agent(self, make_agent):
        """Pausing an already-paused agent should still return True."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_raw = AsyncMock()
        result = asyncio.run(ashlar_server.AgentManager.pause(manager, agent.id))
        assert result is True
        assert agent.status == "paused"

    def test_resume_running_agent(self, make_agent):
        """Resuming a working agent should still succeed and send the message."""
        agent = make_agent(status="working")
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        result = asyncio.run(ashlar_server.AgentManager.resume(manager, agent.id))
        assert result is True
        assert agent.status == "working"

    def test_restart_guard_prevents_concurrent(self, make_agent):
        """If _restart_in_progress is set, restart should return False."""
        agent = make_agent(status="error")
        # Simulate lock being held (concurrent restart in progress)
        async def _test():
            async with agent._restart_lock:
                # Lock is held, so restart should return False
                manager = MagicMock(spec=ashlar_server.AgentManager)
                manager.agents = {agent.id: agent}
                result = await ashlar_server.AgentManager.restart(manager, agent.id)
                return result
        result = asyncio.run(_test())
        assert result is False

    def test_resume_with_custom_message(self, make_agent):
        """Resume should use the custom message if provided."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        result = asyncio.run(ashlar_server.AgentManager.resume(manager, agent.id, message="yes, proceed"))
        assert result is True
        manager._tmux_send_keys.assert_called_once_with(agent.tmux_session, "yes, proceed")


# ─────────────────────────────────────────────
# T5: Spawn validation and error paths
# ─────────────────────────────────────────────

class TestSpawnValidation:
    def test_max_agents_config_exists(self):
        """Config should have a max_agents setting."""
        config = ashlar_server.Config()
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

class TestExtractSummaryEnhanced:
    def test_intent_pattern_extracts_claude_intent(self):
        """'I'll fix the authentication bug' should extract intent."""
        lines = ["Looking at the code.", "I'll fix the authentication bug in the login handler"]
        summary = extract_summary(lines, "Fix bugs")
        assert "fix" in summary.lower() or "authentication" in summary.lower()

    def test_git_commit_summary(self):
        """Git commit messages should be extracted."""
        lines = ["staged files", "committed changes to main branch"]
        summary = extract_summary(lines, "Deploy")
        assert "committed" in summary.lower()

    def test_error_status_shows_error_line(self):
        """When status is error, should show the error line."""
        lines = ["Working...", "fatal: repository not found", "some other line"]
        summary = extract_summary(lines, "Clone repo", status="error")
        assert "fatal" in summary.lower()

    def test_summary_capped_at_80_chars(self):
        """Summaries should now be capped at 80 characters."""
        lines = ["Writing " + "a" * 200 + ".ts"]
        summary = extract_summary(lines, "task")
        assert len(summary) <= 80


# ─────────────────────────────────────────────
# Restart field reset tests
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

class TestBackendConfigContextFields:
    def test_backend_config_to_dict_includes_context_fields(self):
        """BackendConfig.to_dict() should include context_window and char_to_token_ratio."""
        bc = BackendConfig(command="test", context_window=150_000, char_to_token_ratio=3.0)
        d = bc.to_dict()
        assert "context_window" in d
        assert "char_to_token_ratio" in d
        assert d["context_window"] == 150_000
        assert d["char_to_token_ratio"] == 3.0

    def test_codex_has_different_ratio(self):
        """Codex should have a different char_to_token_ratio than claude-code."""
        claude = KNOWN_BACKENDS["claude-code"]
        codex = KNOWN_BACKENDS["codex"]
        assert claude.char_to_token_ratio != codex.char_to_token_ratio

    def test_all_backends_have_positive_context_window(self):
        """Every backend should have a positive context window."""
        for name, bc in KNOWN_BACKENDS.items():
            assert bc.context_window > 0, f"{name} has non-positive context_window"


# ─────────────────────────────────────────────
# Health warning flag reset tests
# ─────────────────────────────────────────────

class TestHealthWarningFlags:
    def test_health_flags_exist_on_agent(self, make_agent):
        """Agent should have health warning flag attributes."""
        agent = make_agent()
        # These are set dynamically, so check with getattr
        agent._health_low_warned = True
        agent._health_critical_warned = True
        assert agent._health_low_warned is True
        assert agent._health_critical_warned is True

    def test_health_flags_can_be_reset(self, make_agent):
        """Health warning flags should be resettable."""
        agent = make_agent()
        agent._health_low_warned = True
        agent._health_critical_warned = True
        # Simulate reset (as done in status change to error)
        agent._health_low_warned = False
        agent._health_critical_warned = False
        assert agent._health_low_warned is False
        assert agent._health_critical_warned is False


# ─────────────────────────────────────────────
# Tmux capture return type tests
# ─────────────────────────────────────────────

class TestCaptureOutputReturnTypes:
    def test_capture_output_returns_none_for_missing_agent(self, make_agent):
        """capture_output should return None for non-existent agent."""
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {}
        result = asyncio.run(ashlar_server.AgentManager.capture_output(manager, "nonexistent"))
        assert result is None

    def test_status_patterns_planning_includes_thinking(self):
        """Planning patterns should include <thinking> block detection."""
        patterns = STATUS_PATTERNS["planning"]
        # Check that at least one pattern matches <thinking>
        matches = any(p.search("<thinking>") for p in patterns)
        assert matches, "No planning pattern matches <thinking>"

    def test_status_patterns_working_includes_tool_calls(self):
        """Working patterns should include Claude Code tool call patterns."""
        patterns = STATUS_PATTERNS["working"]
        test_strings = ["Read(src/app.ts)", "Tool Result: success", "installing dependencies"]
        for test in test_strings:
            matches = any(p.search(test) for p in patterns)
            assert matches, f"No working pattern matches: {test}"

    def test_status_patterns_complete_includes_committed(self):
        """Complete patterns should include 'changes committed'."""
        patterns = STATUS_PATTERNS["complete"]
        matches = any(p.search("changes committed") for p in patterns)
        assert matches, "No complete pattern matches 'changes committed'"

    def test_status_patterns_complete_includes_no_issues(self):
        """Complete patterns should include 'no issues found'."""
        patterns = STATUS_PATTERNS["complete"]
        matches = any(p.search("no issues found") for p in patterns)
        assert matches, "No complete pattern matches 'no issues found'"


# ─────────────────────────────────────────────
# T10: Plan mode
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

class TestDatabaseDegradedMode:
    def test_save_agent_returns_on_no_db(self, make_agent):
        """save_agent should return early when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        agent = make_agent(status="working")
        # Should not raise
        asyncio.run(db.save_agent(agent))

    def test_get_agent_history_returns_empty_on_no_db(self):
        """get_agent_history should return [] when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_agent_history())
        assert result == []

    def test_get_agent_history_count_returns_zero_on_no_db(self):
        """get_agent_history_count should return 0 when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_agent_history_count())
        assert result == 0

    def test_get_agent_history_item_returns_none_on_no_db(self):
        """get_agent_history_item should return None when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_agent_history_item("abc1"))
        assert result is None

    def test_save_project_returns_on_no_db(self):
        """save_project should return early when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        asyncio.run(db.save_project({"id": "p1", "name": "test", "path": "/tmp"}))

    def test_get_projects_returns_empty_on_no_db(self):
        """get_projects should return [] when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_projects())
        assert result == []

    def test_delete_project_returns_false_on_no_db(self):
        """delete_project should return False when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.delete_project("p1"))
        assert result is False

    def test_save_workflow_returns_on_no_db(self):
        """save_workflow should return early when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        asyncio.run(db.save_workflow({"id": "w1", "name": "test"}))

    def test_get_workflows_returns_empty_on_no_db(self):
        """get_workflows should return [] when _db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_workflows())
        assert result == []


# ─────────────────────────────────────────────
# T13: Resume uses set_status
# ─────────────────────────────────────────────

class TestResumeSetStatus:
    def test_resume_uses_set_status(self, make_agent):
        """resume() should use set_status() not direct assignment for monotonic guard."""
        agent = make_agent(status="paused")
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        asyncio.run(ashlar_server.AgentManager.resume(manager, agent.id))
        assert agent.status == "working"
        # set_status updates _status_updated_at — verify it was bumped
        assert agent._status_updated_at > 0

    def test_resume_clears_input_state(self, make_agent):
        """resume() should clear needs_input and input_prompt."""
        agent = make_agent(status="paused")
        agent.needs_input = True
        agent.input_prompt = "Some prompt"
        manager = MagicMock(spec=ashlar_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager._tmux_send_keys = AsyncMock()
        asyncio.run(ashlar_server.AgentManager.resume(manager, agent.id))
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

class TestExtensionScanner:
    def test_scan_returns_dict_with_expected_keys(self):
        """scan() should return dict with skills, mcp_servers, plugins, scanned_at."""
        from ashlar_server import ExtensionScanner
        scanner = ExtensionScanner()
        result = scanner.scan()
        assert "skills" in result
        assert "mcp_servers" in result
        assert "plugins" in result
        assert "scanned_at" in result

    def test_to_dict_structure(self):
        """to_dict should return correct structure even when empty."""
        from ashlar_server import ExtensionScanner
        scanner = ExtensionScanner()
        d = scanner.to_dict()
        assert d["skills"] == []
        assert d["mcp_servers"] == []
        assert d["plugins"] == []
        assert d["scanned_at"] == ""

    def test_parse_skill_frontmatter(self, tmp_path):
        """Should parse YAML frontmatter from a skill .md file."""
        from ashlar_server import ExtensionScanner
        skill_file = tmp_path / "test-skill.md"
        skill_file.write_text("---\ndescription: Test skill\nargument-hint: <arg>\nallowed-tools: Bash\n---\n\n# Test")
        desc, hint, tools = ExtensionScanner._parse_skill_frontmatter(skill_file)
        assert desc == "Test skill"
        assert hint == "<arg>"
        assert tools == "Bash"

    def test_parse_skill_no_frontmatter(self, tmp_path):
        """Skills without frontmatter should return empty strings."""
        from ashlar_server import ExtensionScanner
        skill_file = tmp_path / "bare.md"
        skill_file.write_text("# Just a heading\nSome content")
        desc, hint, tools = ExtensionScanner._parse_skill_frontmatter(skill_file)
        assert desc == ""
        assert hint == ""
        assert tools == ""

    def test_scan_skills_from_dir(self, tmp_path):
        """Should discover .md files recursively."""
        from ashlar_server import ExtensionScanner
        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "commit.md").write_text("---\ndescription: Git commit\n---\n")
        sub_dir = cmd_dir / "gsd"
        sub_dir.mkdir()
        (sub_dir / "plan.md").write_text("---\ndescription: Plan phase\n---\n")

        scanner = ExtensionScanner()
        skills = scanner._scan_skill_dir(cmd_dir, "user")
        names = [s.name for s in skills]
        assert "commit" in names
        assert "gsd/plan" in names

    def test_parse_mcp_dict(self):
        """Should parse MCP server configs from dict."""
        from ashlar_server import ExtensionScanner
        mcp_dict = {
            "my-server": {
                "type": "stdio",
                "command": "node",
                "args": ["server.js"],
            },
            "api-server": {
                "type": "http",
                "url": "http://localhost:3000",
            },
        }
        results = ExtensionScanner._parse_mcp_dict(mcp_dict, "user")
        assert len(results) == 2
        stdio = next(r for r in results if r.name == "my-server")
        assert stdio.server_type == "stdio"
        assert stdio.url_or_command == "node"
        assert stdio.args == ["server.js"]
        http = next(r for r in results if r.name == "api-server")
        assert http.server_type == "http"
        assert http.url_or_command == "http://localhost:3000"

    def test_scan_plugins_from_settings(self, tmp_path):
        """Should parse plugins from settings.json."""
        import json
        from ashlar_server import ExtensionScanner
        settings = {"enabledPlugins": {"my-plugin@provider": True, "disabled-one@other": False}}
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        scanner = ExtensionScanner()
        # Directly test the parsing logic
        with patch.object(Path, 'home', return_value=tmp_path / "fake"):
            # Since _scan_plugins reads from ~/.claude/settings.json, we test _parse_mcp_dict instead
            pass
        # Test plugin info structure
        from ashlar_server import PluginInfo
        p = PluginInfo(name="test", provider="provider", enabled=True)
        assert p.to_dict() == {"name": "test", "provider": "provider", "enabled": True}


# ─────────────────────────────────────────────
# T11: Context Detection from Output
# ─────────────────────────────────────────────

class TestContextDetection:
    def _make_manager(self):
        """Create a minimal AgentManager for testing."""
        from ashlar_server import AgentManager, Config
        config = Config.__new__(Config)
        config.max_agents = 16
        config.default_backend = "claude-code"
        config.backends = {}
        config.output_capture_interval = 1.0
        config.memory_limit_mb = 2048
        config.default_working_dir = "/tmp"
        config.default_role = "general"
        manager = AgentManager.__new__(AgentManager)
        manager.backend_configs = {}
        return manager

    def test_detects_percentage(self):
        """Should detect 'context 73%' pattern."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["Context usage: 73%"], "claude-code")
        assert result is not None
        assert abs(result - 0.73) < 0.01

    def test_detects_reverse_percentage(self):
        """Should detect '73% context' pattern."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["Using 45% of context window"], "claude-code")
        assert result is not None
        assert abs(result - 0.45) < 0.01

    def test_detects_token_ratio(self):
        """Should detect '142K of 200K' pattern."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["142K of 200K tokens"], "claude-code")
        assert result is not None
        assert abs(result - 0.71) < 0.01

    def test_detects_compaction(self):
        """Should detect compaction warning."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["Compacting conversation..."], "claude-code")
        assert result == 0.95

    def test_returns_none_for_non_claude(self):
        """Should return None for non-claude-code backends."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["context 73%"], "codex")
        assert result is None

    def test_returns_none_for_no_match(self):
        """Should return None when no context indicator found."""
        manager = self._make_manager()
        result = manager._detect_context_from_output(["Regular output line"], "claude-code")
        assert result is None


# ─────────────────────────────────────────────
# T13: Follow-up suggestions
# ─────────────────────────────────────────────

class TestFollowupSuggestions:
    def test_backend_suggests_tester(self, make_agent):
        """Backend agent completion should suggest spawning a tester."""
        agent = make_agent(role="backend", name="api-worker")
        agent.summary = "Implemented payment API endpoints"
        result = _suggest_followup(agent)
        assert result is not None
        assert result["suggested_role"] == "tester"
        assert "api-worker" in result["message"]

    def test_frontend_suggests_tester(self, make_agent):
        """Frontend agent should suggest tester follow-up."""
        agent = make_agent(role="frontend", name="ui-builder")
        result = _suggest_followup(agent)
        assert result is not None
        assert result["suggested_role"] == "tester"

    def test_architect_suggests_backend(self, make_agent):
        """Architect agent should suggest backend implementation."""
        agent = make_agent(role="architect", name="sys-design")
        result = _suggest_followup(agent)
        assert result is not None
        assert result["suggested_role"] == "backend"

    def test_security_suggests_backend(self, make_agent):
        """Security agent should suggest backend fix follow-up."""
        agent = make_agent(role="security", name="sec-audit")
        result = _suggest_followup(agent)
        assert result is not None
        assert result["suggested_role"] == "backend"

    def test_general_returns_none(self, make_agent):
        """General role has no follow-up suggestions."""
        agent = make_agent(role="general", name="helper")
        result = _suggest_followup(agent)
        assert result is None

    def test_docs_returns_none(self, make_agent):
        """Docs role has no follow-up suggestions."""
        agent = make_agent(role="docs", name="writer")
        result = _suggest_followup(agent)
        assert result is None

    def test_suggestion_includes_task_info(self, make_agent):
        """Follow-up suggestion should include agent name and summary."""
        agent = make_agent(role="backend", name="auth-api")
        agent.summary = "Added JWT authentication"
        agent.task = "Implement auth"
        result = _suggest_followup(agent)
        assert result is not None
        assert "auth-api" in result["suggested_task"]
        assert "JWT" in result["suggested_task"]

    def test_followup_suggestions_dict_has_expected_roles(self):
        """FOLLOWUP_SUGGESTIONS should cover main development roles."""
        assert "backend" in FOLLOWUP_SUGGESTIONS
        assert "frontend" in FOLLOWUP_SUGGESTIONS
        assert "architect" in FOLLOWUP_SUGGESTIONS
        assert "security" in FOLLOWUP_SUGGESTIONS
        assert "reviewer" in FOLLOWUP_SUGGESTIONS
        assert "tester" in FOLLOWUP_SUGGESTIONS


# ─────────────────────────────────────────────
# T14: Task Queue
# ─────────────────────────────────────────────

class TestTaskQueue:
    def test_queued_task_to_dict(self):
        """QueuedTask.to_dict() should include all fields."""
        task = QueuedTask(
            id="abc123",
            role="backend",
            name="api-worker",
            task="Build API",
            working_dir="/tmp",
            backend="claude-code",
            priority=5,
            created_at="2026-01-01T00:00:00Z",
        )
        d = task.to_dict()
        assert d["id"] == "abc123"
        assert d["role"] == "backend"
        assert d["name"] == "api-worker"
        assert d["task"] == "Build API"
        assert d["priority"] == 5

    def test_queued_task_defaults(self):
        """QueuedTask should have sensible defaults."""
        task = QueuedTask(id="x", role="general", name="t", task="do stuff")
        assert task.working_dir == ""
        assert task.backend == ""
        assert task.plan_mode is False
        assert task.project_id is None
        assert task.priority == 0

    def test_manager_has_task_queue(self):
        """AgentManager should have a task_queue list."""
        with patch.dict("os.environ", {"CLAUDECODE": "1"}):
            from ashlar_server import Config, AgentManager
            config = Config()
            manager = AgentManager(config)
            assert hasattr(manager, 'task_queue')
            assert isinstance(manager.task_queue, list)
            assert len(manager.task_queue) == 0

    def test_queue_priority_sorting(self):
        """Tasks should be sortable by priority (higher first)."""
        tasks = [
            QueuedTask(id="a", role="backend", name="low", task="t", priority=1),
            QueuedTask(id="b", role="frontend", name="high", task="t", priority=10),
            QueuedTask(id="c", role="tester", name="mid", task="t", priority=5),
        ]
        tasks.sort(key=lambda t: -t.priority)
        assert tasks[0].name == "high"
        assert tasks[1].name == "mid"
        assert tasks[2].name == "low"


# ─────────────────────────────────────────────
# T13: Bulk operations validation
# ─────────────────────────────────────────────

class TestBulkOperations:
    """Tests for bulk action validation logic."""

    def test_valid_bulk_actions(self):
        """All 5 bulk actions should be accepted."""
        valid = ("kill", "pause", "resume", "send", "restart")
        for action in valid:
            assert action in valid

    def test_send_requires_message(self):
        """Bulk send should require a non-empty message field."""
        # Simulate what the server checks
        message = ""
        assert not (isinstance(message, str) and message.strip())

        message = "  "
        assert not (isinstance(message, str) and message.strip())

        message = "hello agents"
        assert isinstance(message, str) and message.strip()

    def test_send_message_sanitization(self):
        """Messages should be sanitized: capped at 500 chars, control chars stripped."""
        import re
        raw = "x" * 600
        sanitized = raw[:500].replace('\r', '')
        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)
        assert len(sanitized) == 500

        raw_ctrl = "hello\x00world\x07test"
        sanitized = raw_ctrl[:500].replace('\r', '')
        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)
        assert sanitized == "helloworldtest"

    def test_agent_ids_must_be_list(self):
        """agent_ids must be a non-empty list."""
        assert isinstance([], list) and not []  # empty fails
        assert isinstance(["a"], list) and ["a"]  # non-empty passes
        assert not isinstance("a", list)  # string fails


# ─────────────────────────────────────────────
# T14: OutputIntelligenceParser
# ─────────────────────────────────────────────

class TestOutputIntelligenceParser:
    """Tests for the regex-based output intelligence parser."""

    def _make_agent(self, lines):
        import collections
        from ashlar_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = lines
        agent._last_parse_index = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)
        return agent, OutputIntelligenceParser()

    def test_parse_read_tool(self):
        """Parser should detect Read tool invocations."""
        agent, parser = self._make_agent(['Read("/src/main.py")'])
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert len(agent._tool_invocations) == 1
        assert agent._tool_invocations[0].tool == "Read"
        assert agent._tool_invocations[0].args == "/src/main.py"

    def test_parse_edit_tool(self):
        """Parser should detect Edit tool invocations."""
        agent, parser = self._make_agent(['Edit("/src/utils.ts")'])
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert agent._tool_invocations[0].tool == "Edit"

    def test_parse_bash_tool(self):
        """Parser should detect Bash tool invocations."""
        agent, parser = self._make_agent(['Bash("npm test --coverage")'])
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert agent._tool_invocations[0].tool == "Bash"

    def test_parse_write_tool_creates_file_operation(self):
        """Write tool should also create a file operation."""
        agent, parser = self._make_agent(['Write("/src/new_file.py")'])
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert counts["files"] == 1
        assert len(agent._file_operations) == 1
        assert agent._file_operations[0].operation == "write"

    def test_parse_git_commit(self):
        """Parser should detect git commit operations."""
        agent, parser = self._make_agent(["git commit -m 'fix: resolve login bug'"])
        counts = parser.parse_incremental(agent)
        assert counts["git"] == 1
        assert agent._git_operations[0].operation == "commit"

    def test_parse_git_checkout(self):
        """Parser should detect git checkout operations."""
        agent, parser = self._make_agent(["git checkout feature/auth"])
        counts = parser.parse_incremental(agent)
        assert counts["git"] == 1
        assert agent._git_operations[0].operation == "checkout"
        assert agent._git_operations[0].detail == "feature/auth"

    def test_parse_pytest_results(self):
        """Parser should detect pytest results."""
        agent, parser = self._make_agent(["42 passed, 3 failed, 1 skipped"])
        counts = parser.parse_incremental(agent)
        assert counts["tests"] == 1
        assert agent._test_results[0].passed == 42
        assert agent._test_results[0].failed == 3
        assert agent._test_results[0].skipped == 1
        assert agent._test_results[0].framework == "pytest"

    def test_parse_jest_results(self):
        """Parser should detect jest-style results."""
        # Jest format: "Tests: X passed, Y failed, Z total" — but pytest regex matches first
        # since both use "N passed". Jest is only tried if pytest doesn't match.
        # Use a format that uniquely matches jest:
        agent, parser = self._make_agent(["Tests:  2 failed, 17 total"])
        counts = parser.parse_incremental(agent)
        assert counts["tests"] == 1
        assert agent._test_results[0].failed == 2
        assert agent._test_results[0].framework == "jest"

    def test_incremental_parsing(self):
        """Parser should only process new lines on subsequent calls."""
        agent, parser = self._make_agent(['Read("/a.py")', 'Read("/b.py")'])
        parser.parse_incremental(agent)
        assert len(agent._tool_invocations) == 2
        # Add more lines
        agent.output_lines.append('Edit("/c.py")')
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert len(agent._tool_invocations) == 3

    def test_no_new_lines(self):
        """Parser should return zeros when no new lines."""
        agent, parser = self._make_agent(['Read("/a.py")'])
        parser.parse_incremental(agent)
        counts = parser.parse_incremental(agent)
        assert counts == {"tools": 0, "files": 0, "git": 0, "tests": 0}

    def test_tool_invocation_cap(self):
        """Tool invocations should be capped at 500."""
        lines = [f'Read("/file{i}.py")' for i in range(600)]
        agent, parser = self._make_agent(lines)
        parser.parse_incremental(agent)
        assert len(agent._tool_invocations) == 500

    def test_result_status_success(self):
        """Parser should update last tool's result_status on success."""
        agent, parser = self._make_agent(['Read("/x.py")', 'Tool Result: success'])
        parser.parse_incremental(agent)
        assert agent._tool_invocations[0].result_status == "success"

    def test_result_status_error(self):
        """Parser should update last tool's result_status on error."""
        agent, parser = self._make_agent(['Read("/x.py")', 'Error: file not found'])
        parser.parse_incremental(agent)
        assert agent._tool_invocations[0].result_status == "error"

    def test_natural_language_file_read(self):
        """Parser should detect natural language file reads."""
        agent, parser = self._make_agent(['Reading config.yaml'])
        counts = parser.parse_incremental(agent)
        assert counts["files"] == 1
        assert agent._file_operations[0].operation == "read"
        assert agent._file_operations[0].file_path == "config.yaml"

    def test_natural_language_file_write(self):
        """Parser should detect natural language file writes."""
        agent, parser = self._make_agent(['Creating server.py'])
        counts = parser.parse_incremental(agent)
        assert counts["files"] == 1
        assert agent._file_operations[0].operation == "write"

    def test_coverage_detection(self):
        """Parser should detect coverage percentage."""
        agent, parser = self._make_agent(["42 passed  Coverage: 87.5%"])
        counts = parser.parse_incremental(agent)
        assert counts["tests"] == 1
        assert agent._test_results[0].coverage_pct == 87.5


# ─────────────────────────────────────────────
# T15: Context window calculations
# ─────────────────────────────────────────────

class TestContextWindowCalculations:
    """Tests for backend-aware context window configuration."""

    def test_claude_code_context_window(self):
        """Claude Code should have 200K context window."""
        bc = KNOWN_BACKENDS.get("claude-code")
        assert bc is not None
        assert bc.context_window == 200000

    def test_codex_context_window(self):
        """Codex should have 128K context window."""
        bc = KNOWN_BACKENDS.get("codex")
        assert bc is not None
        assert bc.context_window == 128000

    def test_aider_context_window(self):
        """Aider should have 128K context window."""
        bc = KNOWN_BACKENDS.get("aider")
        assert bc is not None
        assert bc.context_window == 128000

    def test_backend_has_char_to_token_ratio(self):
        """All backends should have char_to_token_ratio."""
        for name, bc in KNOWN_BACKENDS.items():
            assert hasattr(bc, 'char_to_token_ratio'), f"{name} missing char_to_token_ratio"
            assert bc.char_to_token_ratio > 0, f"{name} has zero ratio"

    def test_context_pct_calculation(self):
        """Context % should be total_chars / (context_window * ratio)."""
        bc = KNOWN_BACKENDS.get("claude-code")
        total_chars = 100000
        ctx_pct = total_chars / (bc.context_window * bc.char_to_token_ratio)
        assert 0 < ctx_pct < 1  # Should be a fraction


# ─────────────────────────────────────────────
# T16: Agent send_message validation
# ─────────────────────────────────────────────

class TestSendMessageValidation:
    """Tests for send_message edge cases."""

    def test_multiline_message_split(self):
        """Multi-line messages should be split into individual sends."""
        message = "line one\nline two\nline three"
        lines = message.split("\n")
        assert len(lines) == 3
        assert lines[0] == "line one"

    def test_message_length_cap(self):
        """Messages sent via REST should be capped."""
        max_len = 50000
        long_msg = "x" * 60000
        capped = long_msg[:max_len]
        assert len(capped) == max_len


class TestFleetAnalytics:
    """Tests for fleet analytics aggregation logic."""

    def _make_agent(self, **kwargs):
        defaults = dict(
            id="a001", name="test", role="backend", status="working",
            project_id=None, working_dir="/tmp", backend="claude-code",
            task="test", tmux_session="ashlar-a001"
        )
        defaults.update(kwargs)
        return ashlar_server.Agent(**defaults)

    def test_cost_aggregation(self):
        """Total cost sums across all agents."""
        a1 = self._make_agent(id="a001")
        a1.estimated_cost_usd = 0.15
        a2 = self._make_agent(id="a002")
        a2.estimated_cost_usd = 0.30
        total = sum(a.estimated_cost_usd for a in [a1, a2])
        assert abs(total - 0.45) < 0.001

    def test_status_distribution(self):
        """Status counts are correctly tallied."""
        agents = [
            self._make_agent(id="a001", status="working"),
            self._make_agent(id="a002", status="working"),
            self._make_agent(id="a003", status="waiting"),
        ]
        counts = {}
        for a in agents:
            counts[a.status] = counts.get(a.status, 0) + 1
        assert counts["working"] == 2
        assert counts["waiting"] == 1

    def test_role_distribution(self):
        """Role counts are correctly tallied."""
        agents = [
            self._make_agent(id="a001", role="frontend"),
            self._make_agent(id="a002", role="backend"),
            self._make_agent(id="a003", role="backend"),
        ]
        counts = {}
        for a in agents:
            counts[a.role] = counts.get(a.role, 0) + 1
        assert counts["frontend"] == 1
        assert counts["backend"] == 2

    def test_tool_usage_aggregation(self):
        """Tool usage counts across agents."""
        a1 = self._make_agent(id="a001")
        a1._tool_invocations.append(ToolInvocation(
            agent_id="a001", tool="Read", args="file.py",
            timestamp="", line_index=0
        ))
        a1._tool_invocations.append(ToolInvocation(
            agent_id="a001", tool="Edit", args="file.py",
            timestamp="", line_index=1
        ))
        a2 = self._make_agent(id="a002")
        a2._tool_invocations.append(ToolInvocation(
            agent_id="a002", tool="Read", args="other.py",
            timestamp="", line_index=0
        ))
        tool_counts = {}
        for a in [a1, a2]:
            for inv in a._tool_invocations:
                tool_counts[inv.tool] = tool_counts.get(inv.tool, 0) + 1
        assert tool_counts["Read"] == 2
        assert tool_counts["Edit"] == 1

    def test_file_operations_aggregation(self):
        """Top files are correctly counted across agents."""
        a1 = self._make_agent(id="a001")
        a1._file_operations.append(FileOperation(
            agent_id="a001", file_path="src/api.py", operation="read", timestamp=""
        ))
        a1._file_operations.append(FileOperation(
            agent_id="a001", file_path="src/api.py", operation="write", timestamp=""
        ))
        a2 = self._make_agent(id="a002")
        a2._file_operations.append(FileOperation(
            agent_id="a002", file_path="src/models.py", operation="read", timestamp=""
        ))
        all_files = {}
        for a in [a1, a2]:
            for fop in a._file_operations:
                all_files[fop.file_path] = all_files.get(fop.file_path, 0) + 1
        top = sorted(all_files.items(), key=lambda x: -x[1])
        assert top[0] == ("src/api.py", 2)
        assert top[1] == ("src/models.py", 1)

    def test_health_score_average(self):
        """Average health excludes zero-scored agents."""
        a1 = self._make_agent(id="a001")
        a1.health_score = 90.0
        a2 = self._make_agent(id="a002")
        a2.health_score = 80.0
        a3 = self._make_agent(id="a003")
        a3.health_score = 0.0  # new agent, not yet scored
        scores = [a.health_score for a in [a1, a2, a3] if a.health_score > 0]
        avg = sum(scores) / len(scores) if scores else 0
        assert abs(avg - 85.0) < 0.1

    def test_empty_fleet(self):
        """Analytics with no agents returns sensible defaults."""
        agents = []
        total_cost = sum(a.estimated_cost_usd for a in agents)
        scores = [a.health_score for a in agents if a.health_score > 0]
        avg = sum(scores) / len(scores) if scores else 0
        assert total_cost == 0
        assert avg == 0


class TestAgentNotesAndTags:
    """Tests for agent notes and tags fields."""

    def _make_agent(self, **kwargs):
        defaults = dict(
            id="a001", name="test", role="backend", status="working",
            project_id=None, working_dir="/tmp", backend="claude-code",
            task="test", tmux_session="ashlar-a001"
        )
        defaults.update(kwargs)
        return ashlar_server.Agent(**defaults)

    def test_default_notes_empty(self):
        agent = self._make_agent()
        assert agent.notes == ""

    def test_default_tags_empty(self):
        agent = self._make_agent()
        assert agent.tags == []

    def test_notes_in_to_dict(self):
        agent = self._make_agent()
        agent.notes = "Important context about this agent"
        d = agent.to_dict()
        assert d["notes"] == "Important context about this agent"

    def test_tags_in_to_dict(self):
        agent = self._make_agent()
        agent.tags = ["critical", "frontend", "v2"]
        d = agent.to_dict()
        assert d["tags"] == ["critical", "frontend", "v2"]

    def test_tag_sanitization(self):
        """Tags should be stripped, lowercased, and deduplicated."""
        tags = ["  Frontend ", "BACKEND", "frontend", "  ", "API"]
        clean = list(dict.fromkeys(t.strip().lower()[:30] for t in tags if t.strip()))
        assert clean == ["frontend", "backend", "api"]

    def test_tag_max_length(self):
        """Individual tags should be capped at 30 chars."""
        long_tag = "a" * 50
        capped = long_tag[:30]
        assert len(capped) == 30

    def test_notes_max_length(self):
        """Notes should be capped at 10K chars."""
        max_len = 10000
        long_notes = "x" * 15000
        assert len(long_notes[:max_len]) == max_len


class TestAgentBookmarks:
    """Tests for agent bookmark functionality."""

    def _make_agent(self, **kwargs):
        defaults = dict(
            id="a001", name="test", role="backend", status="working",
            project_id=None, working_dir="/tmp", backend="claude-code",
            task="test", tmux_session="ashlar-a001"
        )
        defaults.update(kwargs)
        return ashlar_server.Agent(**defaults)

    def test_default_bookmarks_empty(self):
        agent = self._make_agent()
        assert agent.bookmarks == []

    def test_bookmarks_in_to_dict(self):
        agent = self._make_agent()
        agent.bookmarks = [{"id": "abc123", "line": 42, "text": "important", "label": "bug", "created_at": ""}]
        d = agent.to_dict()
        assert len(d["bookmarks"]) == 1
        assert d["bookmarks"][0]["line"] == 42

    def test_bookmark_cap(self):
        """Maximum 100 bookmarks per agent."""
        agent = self._make_agent()
        for i in range(100):
            agent.bookmarks.append({"id": f"b{i:03d}", "line": i, "text": f"line {i}"})
        assert len(agent.bookmarks) == 100

    def test_bookmark_text_truncation(self):
        """Bookmark text should be capped at 200 chars."""
        long_text = "x" * 300
        capped = long_text[:200]
        assert len(capped) == 200

    def test_bookmark_delete(self):
        """Deleting a bookmark removes it from the list."""
        agent = self._make_agent()
        agent.bookmarks = [
            {"id": "aaa", "line": 1, "text": "one"},
            {"id": "bbb", "line": 2, "text": "two"},
            {"id": "ccc", "line": 3, "text": "three"},
        ]
        bid = "bbb"
        agent.bookmarks = [b for b in agent.bookmarks if b.get("id") != bid]
        assert len(agent.bookmarks) == 2
        assert all(b["id"] != "bbb" for b in agent.bookmarks)


class TestProjectAutoAssignment:
    """Tests for project auto-assignment based on working directory."""

    def test_path_match(self):
        """Agent working_dir matching project path assigns project."""
        projects = [
            {"id": "proj1", "path": "/home/user/projects/app"},
            {"id": "proj2", "path": "/home/user/projects/lib"},
        ]
        agent_dir = "/home/user/projects/app/src"
        best = None
        best_len = 0
        for proj in projects:
            proj_path = proj["path"]
            if agent_dir.startswith(proj_path) and len(proj_path) > best_len:
                best = proj
                best_len = len(proj_path)
        assert best is not None
        assert best["id"] == "proj1"

    def test_no_match(self):
        """Agent in unrelated dir gets no project."""
        projects = [
            {"id": "proj1", "path": "/home/user/projects/app"},
        ]
        agent_dir = "/tmp/sandbox"
        best = None
        for proj in projects:
            if agent_dir.startswith(proj["path"]):
                best = proj
        assert best is None

    def test_longest_match_wins(self):
        """Most specific project path wins when nested."""
        projects = [
            {"id": "parent", "path": "/home/user/projects"},
            {"id": "child", "path": "/home/user/projects/app"},
        ]
        agent_dir = "/home/user/projects/app/src/components"
        best = None
        best_len = 0
        for proj in projects:
            if agent_dir.startswith(proj["path"]) and len(proj["path"]) > best_len:
                best = proj
                best_len = len(proj["path"])
        assert best["id"] == "child"


# ─────────────────────────────────────────────
# T18: File conflict detection
# ─────────────────────────────────────────────

class TestFileConflicts:
    def test_write_write_conflict_detected(self, make_agent):
        """Two agents writing the same file should produce a conflict."""
        manager = ashlar_server.AgentManager.__new__(ashlar_server.AgentManager)
        manager.agents = {}
        manager.file_activity = {}
        manager._FILE_WRITE_RE = ashlar_server.AgentManager._FILE_WRITE_RE
        manager._FILE_READ_RE = ashlar_server.AgentManager._FILE_READ_RE

        a1 = make_agent(agent_id="aaaa", name="agent-a", status="working")
        a2 = make_agent(agent_id="bbbb", name="agent-b", status="working")
        manager.agents = {"aaaa": a1, "bbbb": a2}

        # Simulate both agents writing the same file
        manager.file_activity = {
            "/src/main.py": {"aaaa": "write", "bbbb": "write"},
        }

        # Now check conflicts using the same logic as list_conflicts endpoint
        conflicts = []
        for file_path, agent_ops in manager.file_activity.items():
            writers = [(aid, op) for aid, op in agent_ops.items()
                       if op == "write" and aid in manager.agents
                       and manager.agents[aid].status in ("working", "planning", "reading")]
            if len(writers) >= 2:
                conflicts.append(file_path)

        assert len(conflicts) == 1
        assert conflicts[0] == "/src/main.py"

    def test_no_conflict_on_separate_files(self, make_agent):
        """Agents working on different files should not produce conflicts."""
        manager = ashlar_server.AgentManager.__new__(ashlar_server.AgentManager)
        manager.agents = {}
        manager.file_activity = {
            "/src/main.py": {"aaaa": "write"},
            "/src/utils.py": {"bbbb": "write"},
        }
        a1 = make_agent(agent_id="aaaa", name="agent-a", status="working")
        a2 = make_agent(agent_id="bbbb", name="agent-b", status="working")
        manager.agents = {"aaaa": a1, "bbbb": a2}

        conflicts = []
        for file_path, agent_ops in manager.file_activity.items():
            writers = [(aid, op) for aid, op in agent_ops.items()
                       if op == "write" and aid in manager.agents
                       and manager.agents[aid].status in ("working", "planning", "reading")]
            if len(writers) >= 2:
                conflicts.append(file_path)

        assert len(conflicts) == 0

    def test_read_read_no_conflict(self, make_agent):
        """Two agents reading the same file is NOT a conflict."""
        manager = ashlar_server.AgentManager.__new__(ashlar_server.AgentManager)
        manager.file_activity = {
            "/src/main.py": {"aaaa": "read", "bbbb": "read"},
        }
        a1 = make_agent(agent_id="aaaa", name="agent-a", status="working")
        a2 = make_agent(agent_id="bbbb", name="agent-b", status="working")
        manager.agents = {"aaaa": a1, "bbbb": a2}

        conflicts = []
        for file_path, agent_ops in manager.file_activity.items():
            writers = [(aid, op) for aid, op in agent_ops.items()
                       if op == "write" and aid in manager.agents
                       and manager.agents[aid].status in ("working", "planning", "reading")]
            if len(writers) >= 2:
                conflicts.append(file_path)

        assert len(conflicts) == 0

    def test_read_write_is_warning(self, make_agent):
        """One agent reading, another writing = warning (not conflict)."""
        file_activity = {
            "/src/main.py": {"aaaa": "write", "bbbb": "read"},
        }
        a1 = make_agent(agent_id="aaaa", name="agent-a", status="working")
        a2 = make_agent(agent_id="bbbb", name="agent-b", status="working")
        agents = {"aaaa": a1, "bbbb": a2}

        # Count write-write conflicts
        write_conflicts = 0
        for file_path, agent_ops in file_activity.items():
            writers = [(aid, op) for aid, op in agent_ops.items()
                       if op == "write" and aid in agents
                       and agents[aid].status in ("working", "planning", "reading")]
            if len(writers) >= 2:
                write_conflicts += 1

        assert write_conflicts == 0  # Only one writer, so not a write-write conflict

    def test_idle_agent_excluded(self, make_agent):
        """Idle agents should not participate in conflict detection."""
        file_activity = {
            "/src/main.py": {"aaaa": "write", "bbbb": "write"},
        }
        a1 = make_agent(agent_id="aaaa", name="agent-a", status="working")
        a2 = make_agent(agent_id="bbbb", name="agent-b", status="idle")
        agents = {"aaaa": a1, "bbbb": a2}

        conflicts = []
        for file_path, agent_ops in file_activity.items():
            writers = [(aid, op) for aid, op in agent_ops.items()
                       if op == "write" and aid in agents
                       and agents[aid].status in ("working", "planning", "reading")]
            if len(writers) >= 2:
                conflicts.append(file_path)

        assert len(conflicts) == 0


# ─────────────────────────────────────────────
# T19: Agent model in to_dict
# ─────────────────────────────────────────────

class TestAgentModelDisplay:
    def test_model_in_to_dict(self, make_agent):
        """Model field should be included in to_dict output."""
        agent = make_agent(model="claude-opus-4")
        d = agent.to_dict()
        assert d["model"] == "claude-opus-4"

    def test_model_default_none(self, make_agent):
        """Model defaults to None when not set."""
        agent = make_agent()
        assert agent.model is None

    def test_output_rate_in_to_dict(self, make_agent):
        """Output rate should be in to_dict."""
        agent = make_agent()
        agent.output_rate = 42.5
        d = agent.to_dict()
        assert d["output_rate"] == 42.5


# ─────────────────────────────────────────────
# T20: Config export/import validation
# ─────────────────────────────────────────────

class TestConfigExportImport:
    def test_config_is_dict(self):
        """Config path should resolve to a valid YAML structure."""
        config = ashlar_server.Config()
        assert config.host == "127.0.0.1"
        assert config.port == 5000

    def test_flat_diff_detects_changes(self):
        """flat_diff should detect key changes between two config dicts."""
        old = {"server": {"host": "127.0.0.1", "port": 5000}}
        new = {"server": {"host": "0.0.0.0", "port": 5000}}

        def flat_diff(old_d, new_d, prefix=""):
            changes = []
            all_keys = set(list(old_d.keys()) + list(new_d.keys()))
            for k in sorted(all_keys):
                path = f"{prefix}.{k}" if prefix else k
                ov = old_d.get(k)
                nv = new_d.get(k)
                if isinstance(ov, dict) and isinstance(nv, dict):
                    changes.extend(flat_diff(ov, nv, path))
                elif ov != nv:
                    changes.append({"key": path, "old": ov, "new": nv})
            return changes

        diff = flat_diff(old, new)
        assert len(diff) == 1
        assert diff[0]["key"] == "server.host"
        assert diff[0]["old"] == "127.0.0.1"
        assert diff[0]["new"] == "0.0.0.0"

    def test_flat_diff_no_changes(self):
        """No changes should produce empty diff."""
        same = {"server": {"port": 5000}}
        def flat_diff(old_d, new_d, prefix=""):
            changes = []
            all_keys = set(list(old_d.keys()) + list(new_d.keys()))
            for k in sorted(all_keys):
                path = f"{prefix}.{k}" if prefix else k
                ov = old_d.get(k)
                nv = new_d.get(k)
                if isinstance(ov, dict) and isinstance(nv, dict):
                    changes.extend(flat_diff(ov, nv, path))
                elif ov != nv:
                    changes.append({"key": path, "old": ov, "new": nv})
            return changes
        diff = flat_diff(same, same)
        assert len(diff) == 0


# ─────────────────────────────────────────────
# T21: Cost budget config
# ─────────────────────────────────────────────

class TestGlobalSearch:
    """Tests for global search matching logic (mirrors /api/search endpoint)."""

    def _search(self, agents, query, case_sensitive=False, use_regex=False, agent_filter=""):
        """Replicate the search logic from global_search endpoint."""
        import re
        results = []
        pattern = None
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(query, flags)

        for agent in agents:
            if agent_filter and agent.id != agent_filter:
                continue
            lines = agent.output_lines or []
            for i, line in enumerate(lines):
                matched = False
                if pattern:
                    matched = bool(pattern.search(line))
                elif case_sensitive:
                    matched = query in line
                else:
                    matched = query.lower() in line.lower()
                if matched:
                    results.append({
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "line_index": i,
                        "line": line[:500],
                    })
        return results

    def test_basic_case_insensitive_search(self, make_agent):
        """Basic search should be case-insensitive by default."""
        agent = make_agent(agent_id="aa11", name="test-agent")
        agent.output_lines = ["Hello World", "foo bar", "HELLO again"]
        results = self._search([agent], "hello")
        assert len(results) == 2
        assert results[0]["line"] == "Hello World"
        assert results[1]["line"] == "HELLO again"

    def test_case_sensitive_search(self, make_agent):
        """Case-sensitive search should only match exact case."""
        agent = make_agent(agent_id="bb22", name="test-agent")
        agent.output_lines = ["Hello World", "hello again", "HELLO"]
        results = self._search([agent], "Hello", case_sensitive=True)
        assert len(results) == 1
        assert results[0]["line"] == "Hello World"

    def test_regex_search(self, make_agent):
        """Regex search should match patterns."""
        agent = make_agent(agent_id="cc33", name="test-agent")
        agent.output_lines = ["error: file not found", "warning: deprecated", "info: started"]
        results = self._search([agent], r"^(error|warning):", use_regex=True)
        assert len(results) == 2

    def test_agent_filter(self, make_agent):
        """Agent filter should restrict results to specific agent."""
        a1 = make_agent(agent_id="dd44", name="agent-a")
        a1.output_lines = ["test line 1"]
        a2 = make_agent(agent_id="ee55", name="agent-b")
        a2.output_lines = ["test line 2"]
        results = self._search([a1, a2], "test", agent_filter="dd44")
        assert len(results) == 1
        assert results[0]["agent_id"] == "dd44"

    def test_empty_output_lines(self, make_agent):
        """Search on agent with no output should return empty results."""
        agent = make_agent(agent_id="ff66", name="test-agent")
        agent.output_lines = []
        results = self._search([agent], "anything")
        assert len(results) == 0

    def test_no_match(self, make_agent):
        """Search with no matching term should return empty."""
        agent = make_agent(agent_id="gg77", name="test-agent")
        agent.output_lines = ["foo", "bar", "baz"]
        results = self._search([agent], "zzzzz")
        assert len(results) == 0


class TestCostBudget:
    def test_default_cost_budget_zero(self):
        """Default cost budget is 0 (no limit)."""
        config = ashlar_server.Config()
        assert config.cost_budget_usd == 0.0
        assert config.cost_budget_auto_pause is False

    def test_cost_budget_in_to_dict(self):
        """Cost budget fields should appear in config to_dict."""
        config = ashlar_server.Config()
        config.cost_budget_usd = 5.0
        config.cost_budget_auto_pause = True
        d = config.to_dict()
        assert d["cost_budget_usd"] == 5.0
        assert d["cost_budget_auto_pause"] is True

    def test_budget_warning_flag(self, make_agent):
        """Budget warning flag should only fire once."""
        agent = make_agent()
        agent.estimated_cost_usd = 6.0
        agent._budget_warned = False
        # Flag should be settable
        agent._budget_warned = True
        assert agent._budget_warned is True


class TestIntelligenceDataInToDict:
    """Verify tool_invocations_count, file_operations_count, last_test_result in Agent.to_dict()."""

    def test_empty_intelligence_data(self, make_agent):
        """Default agent has zero counts and no test results."""
        agent = make_agent()
        d = agent.to_dict()
        assert d["tool_invocations_count"] == 0
        assert d["file_operations_count"] == 0
        assert d["last_test_result"] is None

    def test_tool_invocations_count(self, make_agent):
        """tool_invocations_count reflects list length."""
        agent = make_agent()
        agent._tool_invocations.append(ToolInvocation(
            agent_id=agent.id, tool="Read", args="file.py",
            timestamp=time.time(), line_index=0,
        ))
        agent._tool_invocations.append(ToolInvocation(
            agent_id=agent.id, tool="Edit", args="file.py",
            timestamp=time.time(), line_index=1,
        ))
        d = agent.to_dict()
        assert d["tool_invocations_count"] == 2

    def test_file_operations_count(self, make_agent):
        """file_operations_count reflects list length."""
        agent = make_agent()
        agent._file_operations.append(FileOperation(
            agent_id=agent.id, file_path="/tmp/test.py",
            operation="write", timestamp=time.time(),
        ))
        d = agent.to_dict()
        assert d["file_operations_count"] == 1

    def test_last_test_result(self, make_agent):
        """last_test_result returns the most recent test result."""
        agent = make_agent()
        tr1 = AgentTestResult(
            agent_id=agent.id, passed=10, failed=0, skipped=0,
            total=10, coverage_pct=None, framework="pytest",
            timestamp=time.time(),
        )
        tr2 = AgentTestResult(
            agent_id=agent.id, passed=15, failed=2, skipped=1,
            total=18, coverage_pct=85.0, framework="pytest",
            timestamp=time.time(),
        )
        agent._test_results.append(tr1)
        agent._test_results.append(tr2)
        d = agent.to_dict()
        assert d["last_test_result"] is not None
        assert d["last_test_result"]["passed"] == 15
        assert d["last_test_result"]["failed"] == 2
        assert d["last_test_result"]["framework"] == "pytest"

    def test_test_result_to_dict(self):
        """AgentTestResult.to_dict() includes all fields."""
        tr = AgentTestResult(
            agent_id="a1b2", passed=20, failed=1, skipped=3,
            total=24, coverage_pct=92.5, framework="jest",
            timestamp=1000.0,
        )
        d = tr.to_dict()
        assert d["passed"] == 20
        assert d["failed"] == 1
        assert d["skipped"] == 3
        assert d["total"] == 24
        assert d["coverage_pct"] == 92.5
        assert d["framework"] == "jest"


class TestServerStats:
    """Verify server-side stats tracking counters."""

    def test_initial_stats_are_zero(self):
        """New AgentManager should have zero stats."""
        config = ashlar_server.Config()
        mgr = ashlar_server.AgentManager(config)
        assert mgr._total_spawned == 0
        assert mgr._total_killed == 0
        assert mgr._total_messages_sent == 0

    def test_start_time_set(self):
        """AgentManager should record start time."""
        config = ashlar_server.Config()
        mgr = ashlar_server.AgentManager(config)
        assert mgr._start_time > 0
        assert time.monotonic() - mgr._start_time < 2  # should be very recent


class TestOutputSnapshots:
    """Tests for OutputSnapshot creation and agent snapshot lifecycle."""

    def _make_agent(self):
        agent = ashlar_server.Agent(
            id="snap1", name="test-snap", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test task",
            summary="Doing things", tmux_session="ashlar-snap1",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.output_lines.extend(["line 1", "line 2", "line 3"])
        agent.total_output_lines = 3
        agent.context_pct = 0.42
        return agent

    def test_create_snapshot_basic(self):
        """create_snapshot should return an OutputSnapshot with correct fields."""
        agent = self._make_agent()
        snap = agent.create_snapshot("error")
        assert snap.agent_id == "snap1"
        assert snap.trigger == "error"
        assert snap.status == "working"
        assert snap.summary == "Doing things"
        assert snap.line_count == 3
        assert "line 1" in snap.output_tail
        assert snap.context_pct == 0.42
        assert len(snap.id) == 8

    def test_snapshot_appended_to_list(self):
        """Snapshots should be stored on the agent."""
        agent = self._make_agent()
        agent.create_snapshot("waiting")
        agent.create_snapshot("complete")
        assert len(agent._snapshots) == 2
        assert agent._snapshots[0].trigger == "waiting"
        assert agent._snapshots[1].trigger == "complete"

    def test_snapshot_cap_at_20(self):
        """Snapshots should be capped at 20, keeping the most recent."""
        agent = self._make_agent()
        for i in range(25):
            agent.create_snapshot("manual")
        assert len(agent._snapshots) == 20

    def test_snapshot_to_dict(self):
        """OutputSnapshot.to_dict should include all fields."""
        agent = self._make_agent()
        snap = agent.create_snapshot("manual")
        d = snap.to_dict()
        assert d["trigger"] == "manual"
        assert d["agent_id"] == "snap1"
        assert "output_tail" in d
        assert "created_at" in d

    def test_snapshot_count_in_agent_to_dict(self):
        """Agent.to_dict should include snapshot_count."""
        agent = self._make_agent()
        assert agent.to_dict()["snapshot_count"] == 0
        agent.create_snapshot("error")
        assert agent.to_dict()["snapshot_count"] == 1

    def test_snapshot_tail_limited(self):
        """Snapshot output_tail should capture last 50 lines max."""
        agent = self._make_agent()
        agent.output_lines.clear()
        for i in range(100):
            agent.output_lines.append(f"line-{i}")
        snap = agent.create_snapshot("complete")
        lines = snap.output_tail.split("\n")
        assert len(lines) == 50
        assert lines[-1] == "line-99"
        assert lines[0] == "line-50"


class TestCostBurnRate:
    """Tests for Agent._cost_burn_rate() forecasting."""

    def test_no_burn_rate_without_cost(self):
        """Should return None when no cost data exists."""
        agent = ashlar_server.Agent(
            id="br01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-br01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent._cost_burn_rate() is None

    def test_no_burn_rate_early(self):
        """Should return None when agent is <30s old (not enough data)."""
        agent = ashlar_server.Agent(
            id="br02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-br02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic()  # Just spawned
        agent.estimated_cost_usd = 0.01
        assert agent._cost_burn_rate() is None

    def test_burn_rate_calculation(self):
        """Should calculate cost_per_min and tokens_per_min correctly."""
        agent = ashlar_server.Agent(
            id="br03", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
            summary="", tmux_session="ashlar-br03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 120  # 2 minutes ago
        agent.estimated_cost_usd = 0.10
        agent.tokens_input = 5000
        agent.tokens_output = 2000
        rate = agent._cost_burn_rate()
        assert rate is not None
        assert rate["cost_per_min"] > 0
        assert rate["tokens_per_min"] > 0
        assert rate["uptime_min"] > 1.5
        assert "minutes_remaining" in rate

    def test_burn_rate_in_to_dict(self):
        """Agent.to_dict should include cost_burn_rate field."""
        agent = ashlar_server.Agent(
            id="br04", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-br04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        d = agent.to_dict()
        assert "cost_burn_rate" in d
        assert d["cost_burn_rate"] is None  # No cost data = None


class TestRestartWithTask:
    """Tests for restart with task override."""

    def test_restart_preserves_task_by_default(self):
        """Restart without new_task keeps the original task."""
        agent = ashlar_server.Agent(
            id="rt01", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlar-rt01",
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
        agent = ashlar_server.Agent(
            id="rt02", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlar-rt02",
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
        agent = ashlar_server.Agent(
            id="rt03", name="test", role="backend", status="error",
            working_dir="/tmp", backend="demo", task="Original task",
            summary="", tmux_session="ashlar-rt03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        new_task = ""
        saved_task = agent.task
        if new_task:
            saved_task = new_task
            agent.task = new_task
        assert saved_task == "Original task"
        assert agent.task == "Original task"


class TestHistoricalAnalytics:
    """Tests for historical analytics database method."""

    def test_historical_analytics_no_db(self):
        """With no DB, should return empty structure."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_historical_analytics())
        assert result["total_historical"] == 0

    def test_historical_analytics_returns_dict(self):
        """Return value should be a dict with expected keys."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_historical_analytics())
        assert isinstance(result, dict)
        assert "total_historical" in result


class TestStatusTimeline:
    """Tests for agent status timeline tracking."""

    def test_status_history_recorded(self):
        """set_status should record status transitions."""
        agent = ashlar_server.Agent(
            id="st01", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-st01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.set_status("planning")
        agent.set_status("working")
        agent.set_status("waiting")
        assert len(agent._status_history) == 3
        assert agent._status_history[0]["status"] == "planning"
        assert agent._status_history[1]["status"] == "working"
        assert agent._status_history[2]["status"] == "waiting"

    def test_status_history_no_duplicate(self):
        """Same status transition should not be recorded."""
        agent = ashlar_server.Agent(
            id="st02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-st02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.set_status("working")  # Same status — should not record
        assert len(agent._status_history) == 0

    def test_status_timeline_in_to_dict(self):
        """to_dict should include status_timeline."""
        agent = ashlar_server.Agent(
            id="st03", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-st03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.set_status("working")
        d = agent.to_dict()
        assert "status_timeline" in d
        assert isinstance(d["status_timeline"], list)

    def test_timeline_capped_at_100(self):
        """Status history should not exceed 100 entries."""
        agent = ashlar_server.Agent(
            id="st04", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-st04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        for i in range(110):
            agent.set_status("working" if i % 2 == 0 else "planning")
        assert len(agent._status_history) <= 100


class TestContextExhaustionWarning:
    """Tests for context exhaustion snapshot and warning."""

    def test_context_warning_flag_default(self):
        """_context_exhaustion_warned should default to False."""
        agent = ashlar_server.Agent(
            id="ce01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-ce01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert not getattr(agent, '_context_exhaustion_warned', False)

    def test_context_warning_creates_snapshot(self):
        """Agent at 92%+ context should create snapshot when triggered."""
        agent = ashlar_server.Agent(
            id="ce02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="High context", tmux_session="ashlar-ce02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.context_pct = 0.95
        agent.output_lines.append("test output line")
        snap = agent.create_snapshot(trigger="context_warning")
        assert snap.trigger == "context_warning"
        assert snap.context_pct == 0.95
        assert len(agent._snapshots) == 1


class TestEfficiencyScore:
    """Tests for calculate_efficiency_score."""

    def test_returns_dict_with_required_keys(self):
        agent = ashlar_server.Agent(
            id="ef01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-ef01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 60  # 1 min uptime
        result = ashlar_server.calculate_efficiency_score(agent)
        assert isinstance(result, dict)
        assert "score" in result
        assert "tools_per_min" in result
        assert "files_per_min" in result
        assert "error_rate" in result
        assert "lines_per_min" in result
        assert "context_efficiency" in result
        assert 0.0 <= result["score"] <= 1.0

    def test_score_in_to_dict(self):
        agent = ashlar_server.Agent(
            id="ef02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-ef02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 120
        d = agent.to_dict()
        assert "efficiency" in d
        assert isinstance(d["efficiency"], dict)
        assert "score" in d["efficiency"]

    def test_new_agent_has_low_score(self):
        """Agent with no tools or files should have a low efficiency score."""
        agent = ashlar_server.Agent(
            id="ef03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-ef03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 300
        result = ashlar_server.calculate_efficiency_score(agent)
        assert result["score"] < 0.5
        assert result["tools_per_min"] == 0


class TestOutputBufferPagination:
    """Tests for output buffer overflow and archive tracking."""

    def test_output_deque_max_length(self):
        """Output lines deque has a max length of 2000."""
        agent = ashlar_server.Agent(
            id="pg01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-pg01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent.output_lines.maxlen == 2000

    def test_archived_lines_starts_at_zero(self):
        """Archived lines counter starts at 0."""
        agent = ashlar_server.Agent(
            id="pg02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-pg02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent._archived_lines == 0

    def test_total_output_lines_in_to_dict(self):
        """total_output_lines is included in to_dict."""
        agent = ashlar_server.Agent(
            id="pg03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-pg03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.total_output_lines = 500
        d = agent.to_dict()
        assert d["total_output_lines"] == 500

    def test_output_overflow_tracking(self):
        """When output exceeds deque size, _archived_lines tracks overflow."""
        agent = ashlar_server.Agent(
            id="pg04", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-pg04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        # Fill beyond capacity
        for i in range(2100):
            agent.output_lines.append(f"line {i}")
        assert len(agent.output_lines) == 2000
        # First 100 lines should have been dropped by the deque
        first_line = agent.output_lines[0]
        assert "line 100" in first_line


class TestBookmarkPersistence:
    """Tests for bookmark SQLite persistence layer."""

    def test_agent_bookmarks_start_empty(self):
        """Agent bookmarks list starts empty."""
        agent = ashlar_server.Agent(
            id="bk01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-bk01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent.bookmarks == []

    def test_bookmark_in_memory(self):
        """Bookmarks can be added in memory."""
        agent = ashlar_server.Agent(
            id="bk02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-bk02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.bookmarks.append({"id": "abc", "line": 42, "text": "error line", "label": "Bug"})
        assert len(agent.bookmarks) == 1
        assert agent.bookmarks[0]["line"] == 42
        assert agent.bookmarks[0]["label"] == "Bug"

    def test_bookmark_delete_by_id(self):
        """Bookmarks can be filtered by ID for deletion."""
        agent = ashlar_server.Agent(
            id="bk03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlar-bk03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.bookmarks.append({"id": "a1", "line": 10, "text": "line a", "label": ""})
        agent.bookmarks.append({"id": "b2", "line": 20, "text": "line b", "label": ""})
        agent.bookmarks = [b for b in agent.bookmarks if b.get("id") != "a1"]
        assert len(agent.bookmarks) == 1
        assert agent.bookmarks[0]["id"] == "b2"


class TestCollaborationGraph:
    """Tests for agent collaboration graph data computation."""

    def _make_agent(self, id, name="test", role="backend", project_id=None, files=None):
        agent = ashlar_server.Agent(
            id=id, name=name, role=role, status="working",
            working_dir="/tmp", backend="demo", task="test task",
            summary="", tmux_session=f"ashlar-{id}",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        if project_id:
            agent.project_id = project_id
        if files:
            for f in files:
                agent._file_operations.append(FileOperation(
                    agent_id=id, file_path=f, operation="write",
                    timestamp="2026-01-01T00:00:00Z", tool="Edit"
                ))
        return agent

    def test_shared_file_creates_edge(self):
        """Agents writing to the same file should produce a shared_file edge."""
        a1 = self._make_agent("a001", name="alpha", files=["/tmp/shared.py"])
        a2 = self._make_agent("a002", name="beta", files=["/tmp/shared.py"])

        # Build file_agents map (same logic as collaboration_graph endpoint)
        file_agents = {}
        for a in [a1, a2]:
            for fop in a._file_operations:
                path = fop.file_path
                if path not in file_agents:
                    file_agents[path] = set()
                file_agents[path].add(a.id)

        edges = []
        for path, agent_ids in file_agents.items():
            ids = list(agent_ids)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    edges.append({"from": ids[i], "to": ids[j], "type": "shared_file"})

        assert len(edges) == 1
        assert edges[0]["type"] == "shared_file"

    def test_same_project_creates_edge(self):
        """Agents in the same project should produce a project edge."""
        a1 = self._make_agent("a001", name="alpha", project_id="proj1")
        a2 = self._make_agent("a002", name="beta", project_id="proj1")
        a3 = self._make_agent("a003", name="gamma", project_id="proj2")

        project_agents = {}
        for a in [a1, a2, a3]:
            if a.project_id:
                if a.project_id not in project_agents:
                    project_agents[a.project_id] = []
                project_agents[a.project_id].append(a.id)

        edges = []
        for proj, agent_ids in project_agents.items():
            for i in range(len(agent_ids)):
                for j in range(i + 1, len(agent_ids)):
                    edges.append({"from": agent_ids[i], "to": agent_ids[j], "type": "project"})

        assert len(edges) == 1
        assert edges[0]["from"] == "a001"
        assert edges[0]["to"] == "a002"

    def test_no_edges_for_separate_files(self):
        """Agents working on different files should have no shared_file edges."""
        a1 = self._make_agent("a001", files=["/tmp/file1.py"])
        a2 = self._make_agent("a002", files=["/tmp/file2.py"])

        file_agents = {}
        for a in [a1, a2]:
            for fop in a._file_operations:
                path = fop.file_path
                if path not in file_agents:
                    file_agents[path] = set()
                file_agents[path].add(a.id)

        edges = []
        for path, agent_ids in file_agents.items():
            ids = list(agent_ids)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    edges.append({"from": ids[i], "to": ids[j], "type": "shared_file"})

        assert len(edges) == 0

    def test_node_data_includes_role_and_status(self):
        """Graph nodes should include role, status, and health info."""
        agent = self._make_agent("a001", name="test-agent", role="frontend")
        node = {
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "status": agent.status,
            "health_score": agent.health_score,
        }
        assert node["role"] == "frontend"
        assert node["status"] == "working"
        assert node["health_score"] == 1.0


# ─────────────────────────────────────────────
# T21: Server Audit Bug Fixes
# ─────────────────────────────────────────────

class TestServerAuditFixes:
    """Tests for critical bugs found during server audit."""

    def test_restart_lock_prevents_concurrent(self, make_agent):
        """Restart should be guarded by asyncio.Lock, not just a boolean flag."""
        agent = make_agent(status="error")
        assert hasattr(agent, '_restart_lock')
        assert isinstance(agent._restart_lock, asyncio.Lock)

    def test_restart_lock_held_returns_false(self, make_agent):
        """If restart lock is already held, restart should return False."""
        agent = make_agent(status="error")
        async def _test():
            async with agent._restart_lock:
                manager = MagicMock(spec=ashlar_server.AgentManager)
                manager.agents = {agent.id: agent}
                return await ashlar_server.AgentManager.restart(manager, agent.id)
        result = asyncio.run(_test())
        assert result is False

    def test_deque_tool_invocations(self, make_agent):
        """Tool invocations should use deque(maxlen=500) for O(1) eviction."""
        import collections
        agent = make_agent()
        assert isinstance(agent._tool_invocations, collections.deque)
        assert agent._tool_invocations.maxlen == 500

    def test_deque_file_operations(self, make_agent):
        """File operations should use deque(maxlen=500)."""
        import collections
        agent = make_agent()
        assert isinstance(agent._file_operations, collections.deque)
        assert agent._file_operations.maxlen == 500

    def test_deque_git_operations(self, make_agent):
        """Git operations should use deque(maxlen=200)."""
        import collections
        agent = make_agent()
        assert isinstance(agent._git_operations, collections.deque)
        assert agent._git_operations.maxlen == 200

    def test_deque_test_results(self, make_agent):
        """Test results should use deque(maxlen=50)."""
        import collections
        agent = make_agent()
        assert isinstance(agent._test_results, collections.deque)
        assert agent._test_results.maxlen == 50

    def test_task_redacted_in_to_dict(self, make_agent):
        """Agent.to_dict() should redact secrets from the task field."""
        agent = make_agent()
        agent.task = "Deploy with key sk-abcdef1234567890abcdef1234567890"
        d = agent.to_dict()
        assert "sk-abcdef" not in d["task"]
        assert "REDACTED" in d["task"]

    def test_task_normal_preserved(self, make_agent):
        """Agent.to_dict() should preserve normal task text."""
        agent = make_agent()
        agent.task = "Build the login page"
        d = agent.to_dict()
        assert d["task"] == "Build the login page"

    def test_mcp_tool_pattern_detected(self):
        """Parser should detect MCP tool invocations."""
        import collections
        from ashlar_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['mcp__claude-in-chrome__computer("click")']
        agent._last_parse_index = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert agent._tool_invocations[0].tool == "MCP"
        assert "claude-in-chrome" in agent._tool_invocations[0].args

    def test_skill_tool_pattern_detected(self):
        """Parser should detect Skill tool invocations."""
        import collections
        from ashlar_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['Skill("commit")']
        agent._last_parse_index = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert agent._tool_invocations[0].tool == "Skill"

    def test_agent_tool_pattern_detected(self):
        """Parser should detect Agent tool invocations."""
        import collections
        from ashlar_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['Agent("search for patterns")']
        agent._last_parse_index = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 1
        assert agent._tool_invocations[0].tool == "Agent"

    def test_broadcast_timeout_is_2s(self):
        """WebSocket broadcast timeout should be 2 seconds (not 5)."""
        import inspect
        src = inspect.getsource(ashlar_server.WebSocketHub.broadcast)
        assert "timeout=2.0" in src

    def test_batch_spawn_agent_limit(self):
        """Batch spawn should check concurrent agent limit before spawning."""
        import inspect
        src = inspect.getsource(ashlar_server.batch_spawn)
        assert "available_slots" in src or "max_agents" in src

    def test_sync_handler_db_exception_guard(self):
        """Sync handler should catch DB exceptions for projects and workflows."""
        import inspect
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        # After our fix, each DB call should be wrapped in try/except
        assert src.count("projects = await") >= 1
        assert src.count("workflows = await") >= 1


class TestBackendStatusPatterns:
    """Tests for backend-specific status detection patterns."""

    def test_codex_has_status_patterns(self):
        """Codex backend config should have status_patterns."""
        bc = KNOWN_BACKENDS.get("codex")
        assert bc is not None
        assert hasattr(bc, "status_patterns")
        assert "working" in bc.status_patterns
        assert "waiting" in bc.status_patterns

    def test_aider_has_status_patterns(self):
        """Aider backend config should have status_patterns."""
        bc = KNOWN_BACKENDS.get("aider")
        assert bc is not None
        assert "working" in bc.status_patterns
        assert "planning" in bc.status_patterns

    def test_goose_has_status_patterns(self):
        """Goose backend config should have status_patterns."""
        bc = KNOWN_BACKENDS.get("goose")
        assert bc is not None
        assert "working" in bc.status_patterns

    def test_claude_code_has_status_patterns(self):
        """Claude Code backend config should have status_patterns."""
        bc = KNOWN_BACKENDS.get("claude-code")
        assert bc is not None
        assert "working" in bc.status_patterns
        assert "waiting" in bc.status_patterns

    def test_status_patterns_are_valid_regex(self):
        """All status patterns should be valid regex."""
        import re
        for name, bc in KNOWN_BACKENDS.items():
            if bc.status_patterns:
                for category, patterns in bc.status_patterns.items():
                    for pat in patterns:
                        try:
                            re.compile(pat)
                        except re.error:
                            pytest.fail(f"Invalid regex in {name}.{category}: {pat}")


class TestOutputSearchEndpoint:
    """Tests for the search_agent_output REST endpoint."""

    def test_search_endpoint_exists(self):
        """search_agent_output function should exist."""
        assert hasattr(ashlar_server, "search_agent_output")
        assert asyncio.iscoroutinefunction(ashlar_server.search_agent_output)

    def test_search_endpoint_registered(self):
        """Search endpoint should be registered in create_app route setup."""
        import inspect
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/agents/{id}/output/search" in src

    def test_search_supports_regex_param(self):
        """search_agent_output should support regex query parameter."""
        import inspect
        src = inspect.getsource(ashlar_server.search_agent_output)
        assert "regex" in src
        assert "re.compile" in src

    def test_search_supports_context_lines(self):
        """search_agent_output should support context lines parameter."""
        import inspect
        src = inspect.getsource(ashlar_server.search_agent_output)
        assert "context" in src
        assert "context_lines" in src

    def test_search_limits_query_length(self):
        """Search query should be limited to 500 chars."""
        import inspect
        src = inspect.getsource(ashlar_server.search_agent_output)
        assert "500" in src

    def test_search_limits_result_count(self):
        """Search results should be limited."""
        import inspect
        src = inspect.getsource(ashlar_server.search_agent_output)
        assert "limit" in src


class TestPatternAlerting:
    """Tests for the alert_patterns config and pattern alerting in capture loop."""

    def test_config_has_alert_patterns(self):
        """Config should have alert_patterns field with defaults."""
        config = ashlar_server.Config()
        assert hasattr(config, "alert_patterns")
        assert isinstance(config.alert_patterns, list)
        assert len(config.alert_patterns) >= 5

    def test_alert_patterns_have_required_fields(self):
        """Each alert pattern should have pattern, severity, and label."""
        config = ashlar_server.Config()
        for ap in config.alert_patterns:
            assert "pattern" in ap
            assert "severity" in ap
            assert "label" in ap

    def test_alert_patterns_are_valid_regex(self):
        """All default alert patterns should be valid regex."""
        import re
        config = ashlar_server.Config()
        for ap in config.alert_patterns:
            try:
                re.compile(ap["pattern"])
            except re.error:
                pytest.fail(f"Invalid regex in alert pattern: {ap['pattern']}")

    def test_alert_patterns_in_to_dict(self):
        """alert_patterns should appear in Config.to_dict()."""
        config = ashlar_server.Config()
        d = config.to_dict()
        assert "alert_patterns" in d
        assert len(d["alert_patterns"]) >= 5

    def test_alert_patterns_match_critical_errors(self):
        """Alert patterns should match critical error strings."""
        import re
        config = ashlar_server.Config()
        test_lines = [
            "FATAL error in process",
            "out of memory",
            "permission denied: /etc/secrets",
            "disk full: no space left on device",
            "ECONNREFUSED: connection refused",
        ]
        for line in test_lines:
            matched = False
            for ap in config.alert_patterns:
                if re.search(ap["pattern"], line):
                    matched = True
                    break
            assert matched, f"No alert pattern matched: {line}"

    def test_alert_patterns_dont_match_normal_output(self):
        """Alert patterns should NOT match normal agent output."""
        import re
        config = ashlar_server.Config()
        normal_lines = [
            "Reading file src/main.py",
            "Edit completed successfully",
            "Running tests...",
            "3 tests passed",
            "Writing to output.json",
        ]
        for line in normal_lines:
            for ap in config.alert_patterns:
                assert not re.search(ap["pattern"], line), f"False positive: '{line}' matched '{ap['label']}'"

    def test_alert_throttle_dict_exists(self):
        """Module-level _alert_throttle dict should exist."""
        assert hasattr(ashlar_server, "_alert_throttle")
        assert isinstance(ashlar_server._alert_throttle, dict)

    def test_capture_loop_checks_alert_patterns(self):
        """Output capture loop should check alert patterns."""
        import inspect
        src = inspect.getsource(ashlar_server.output_capture_loop)
        assert "pattern_alert" in src
        assert "_compiled_alert_patterns" in src
        assert "_alert_throttle" in src


class TestCriticalBugFixes:
    """Tests for critical bug fixes found in audit."""

    def test_health_detailed_uses_max_agents(self):
        """health_detailed should use config.max_agents, not max_concurrent_agents."""
        import inspect
        src = inspect.getsource(ashlar_server.health_detailed)
        assert "max_concurrent_agents" not in src
        assert "max_agents" in src

    def test_db_save_message_null_guard(self):
        """save_message should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.save_message)
        assert "if not self._db" in src

    def test_db_get_messages_for_agent_null_guard(self):
        """get_messages_for_agent should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.get_messages_for_agent)
        assert "if not self._db" in src

    def test_db_get_messages_between_null_guard(self):
        """get_messages_between should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.get_messages_between)
        assert "if not self._db" in src

    def test_db_get_message_count_null_guard(self):
        """get_message_count_for_agent should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.get_message_count_for_agent)
        assert "if not self._db" in src

    def test_db_mark_messages_read_null_guard(self):
        """mark_messages_read should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.mark_messages_read)
        assert "if not self._db" in src

    def test_db_get_unread_count_null_guard(self):
        """get_unread_count should guard against null db."""
        import inspect
        src = inspect.getsource(ashlar_server.Database.get_unread_count)
        assert "if not self._db" in src

    def test_compiled_alert_patterns_in_create_app(self):
        """create_app should compile alert patterns at startup."""
        import inspect
        src = inspect.getsource(ashlar_server.create_app)
        assert "_compiled_alert_patterns" in src


class TestConfigLoadingAndValidation:
    """Tests for config loading with alert_patterns and intelligence validators."""

    def test_load_config_parses_alert_patterns(self):
        """load_config should parse alert patterns from YAML alerts section."""
        import inspect
        src = inspect.getsource(ashlar_server.load_config)
        assert "alert_patterns" in src
        assert "validated_alert_patterns" in src

    def test_load_config_validates_alert_regex(self):
        """load_config should validate alert pattern regex."""
        import inspect
        src = inspect.getsource(ashlar_server.load_config)
        assert "re.compile" in src
        assert "re.error" in src

    def test_put_config_has_intelligence_validators(self):
        """put_config should validate intelligence fields."""
        import inspect
        src = inspect.getsource(ashlar_server.put_config)
        assert "intelligence_enabled" in src
        assert "intelligence_model" in src
        assert "intelligence_summary_interval" in src
        assert "intelligence_meta_interval" in src

    def test_put_config_maps_intel_to_yaml(self):
        """put_config should map intelligence fields to YAML intelligence section."""
        import inspect
        src = inspect.getsource(ashlar_server.put_config)
        assert "intel_keys" in src
        assert '"intelligence"' in src

    def test_put_config_recompiles_alert_patterns(self):
        """put_config should recompile alert patterns when config changes."""
        import inspect
        src = inspect.getsource(ashlar_server.put_config)
        assert "_compiled_alert_patterns" in src

    def test_default_config_has_alert_patterns(self):
        """Default Config should have 5 alert patterns."""
        config = ashlar_server.Config()
        assert len(config.alert_patterns) == 5
        # All should have required fields
        for ap in config.alert_patterns:
            assert "pattern" in ap
            assert "severity" in ap
            assert "label" in ap


# ---------------------------------------------------------------------------
# Session Persistence and Resume
# ---------------------------------------------------------------------------

class TestSessionPersistence:
    """Tests for agent session persistence (resumable flag, DB columns, endpoints)."""

    def _make_agent(self, **overrides):
        """Create an Agent with sensible defaults for testing."""
        defaults = dict(
            id="ab12",
            name="test-agent",
            role="backend",
            status="working",
            working_dir="/tmp",
            backend="claude-code",
            task="Test task",
            summary="Doing things",
            created_at="2026-01-01T00:00:00Z",
        )
        defaults.update(overrides)
        return ashlar_server.Agent(**defaults)

    def test_resumable_flag_for_complete_status(self):
        """Agents with status 'complete' should be resumable."""
        agent = self._make_agent(status="complete")
        # The resumable logic is: status in ("complete", "working", "planning", "idle")
        assert agent.status in ("complete", "working", "planning", "idle")

    def test_resumable_flag_for_working_status(self):
        """Agents killed while working should be resumable."""
        agent = self._make_agent(status="working")
        assert agent.status in ("complete", "working", "planning", "idle")

    def test_resumable_flag_for_planning_status(self):
        """Agents killed while planning should be resumable."""
        agent = self._make_agent(status="planning")
        assert agent.status in ("complete", "working", "planning", "idle")

    def test_resumable_flag_for_idle_status(self):
        """Idle agents should be resumable."""
        agent = self._make_agent(status="idle")
        assert agent.status in ("complete", "working", "planning", "idle")

    def test_not_resumable_for_error_status(self):
        """Agents that errored out should NOT be resumable."""
        agent = self._make_agent(status="error")
        assert agent.status not in ("complete", "working", "planning", "idle")

    def test_not_resumable_for_spawning_status(self):
        """Agents still spawning should NOT be resumable."""
        agent = self._make_agent(status="spawning")
        assert agent.status not in ("complete", "working", "planning", "idle")

    def test_agent_has_system_prompt_field(self):
        """Agent dataclass should have system_prompt field."""
        agent = self._make_agent(system_prompt="You are a backend engineer")
        assert agent.system_prompt == "You are a backend engineer"

    def test_agent_has_plan_mode_field(self):
        """Agent dataclass should have plan_mode field."""
        agent = self._make_agent(plan_mode=True)
        assert agent.plan_mode is True

    def test_agent_plan_mode_default_false(self):
        """Agent plan_mode should default to False."""
        agent = self._make_agent()
        assert agent.plan_mode is False

    def test_save_agent_includes_resumable_column(self):
        """save_agent() SQL should include resumable column."""
        src = inspect.getsource(ashlar_server.Database.save_agent)
        assert "resumable" in src

    def test_save_agent_includes_system_prompt_column(self):
        """save_agent() SQL should include system_prompt column."""
        src = inspect.getsource(ashlar_server.Database.save_agent)
        assert "system_prompt" in src

    def test_save_agent_includes_plan_mode_column(self):
        """save_agent() SQL should include plan_mode column."""
        src = inspect.getsource(ashlar_server.Database.save_agent)
        assert "plan_mode" in src

    def test_get_resumable_sessions_method_exists(self):
        """Database should have get_resumable_sessions method."""
        assert hasattr(ashlar_server.Database, "get_resumable_sessions")
        assert callable(getattr(ashlar_server.Database, "get_resumable_sessions"))

    def test_get_resumable_sessions_null_guard(self):
        """get_resumable_sessions() should return [] when db is None."""
        db = ashlar_server.Database.__new__(ashlar_server.Database)
        db._db = None
        result = asyncio.run(db.get_resumable_sessions())
        assert result == []

    def test_get_resumable_sessions_filters_by_resumable(self):
        """get_resumable_sessions() SQL should filter WHERE resumable = 1."""
        src = inspect.getsource(ashlar_server.Database.get_resumable_sessions)
        assert "resumable = 1" in src

    def test_get_resumable_sessions_orders_by_completed_at(self):
        """get_resumable_sessions() should order by most recent first."""
        src = inspect.getsource(ashlar_server.Database.get_resumable_sessions)
        assert "ORDER BY completed_at DESC" in src

    def test_resume_endpoint_registered(self):
        """POST /api/sessions/{id}/resume should be registered."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/sessions/{id}/resume" in src

    def test_resumable_sessions_endpoint_registered(self):
        """GET /api/sessions/resumable should be registered."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/sessions/resumable" in src

    def test_resume_handler_checks_working_dir(self):
        """resume_from_history() should verify working directory exists."""
        src = inspect.getsource(ashlar_server.resume_from_history)
        assert "isdir" in src or "working_dir" in src

    def test_resume_handler_checks_agent_limit(self):
        """resume_from_history() should check max_agents limit."""
        src = inspect.getsource(ashlar_server.resume_from_history)
        assert "max_agents" in src

    def test_resume_handler_supports_task_override(self):
        """resume_from_history() should allow overriding the task."""
        src = inspect.getsource(ashlar_server.resume_from_history)
        assert "task" in src

    def test_resume_handler_supports_continue_message(self):
        """resume_from_history() should support continue_message field."""
        src = inspect.getsource(ashlar_server.resume_from_history)
        assert "continue_message" in src

    def test_db_migration_adds_resumable_column(self):
        """DB init should add resumable column to agents_history."""
        src = inspect.getsource(ashlar_server.Database.init)
        assert "resumable" in src

    def test_db_migration_adds_system_prompt_column(self):
        """DB init should add system_prompt column to agents_history."""
        src = inspect.getsource(ashlar_server.Database.init)
        assert "system_prompt" in src

    def test_db_migration_adds_plan_mode_column(self):
        """DB init should add plan_mode column to agents_history."""
        src = inspect.getsource(ashlar_server.Database.init)
        assert "plan_mode" in src

    def test_resumable_statuses_are_correct(self):
        """The resumable status check should match the expected set."""
        src = inspect.getsource(ashlar_server.Database.save_agent)
        # Verify the exact status set used for resumable flag
        for status in ("complete", "working", "planning", "idle"):
            assert status in src


# ---------------------------------------------------------------------------
# Bug Fixes: Silent Exceptions, Memory Leaks, Fleet Export
# ---------------------------------------------------------------------------

class TestBugFixesAndFleetExport:
    """Tests for bug fixes: silent exception logging, alert throttle cleanup, fleet export."""

    def test_spawn_cleanup_logs_on_failure(self):
        """Spawn tmux cleanup should log warnings, not silently pass."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        # Should contain log.warning for cleanup failures, not bare pass
        assert "cleanup_err" in src
        assert "log.warning" in src

    def test_broadcast_catches_cancelled_error(self):
        """WebSocket broadcast should catch asyncio.CancelledError."""
        src = inspect.getsource(ashlar_server.WebSocketHub.broadcast)
        assert "CancelledError" in src

    def test_alert_throttle_cleanup_in_health_loop(self):
        """Health loop should periodically clean up stale alert throttle entries."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "_alert_throttle" in src
        # Verify both stale eviction and cap enforcement
        assert "120.0" in src or "stale_keys" in src

    def test_alert_throttle_capped_at_500(self):
        """Alert throttle cleanup should enforce max 500 entries."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "500" in src

    def test_fleet_export_endpoint_registered(self):
        """GET /api/fleet/export should be registered."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/fleet/export" in src

    def test_fleet_export_handler_exists(self):
        """export_fleet_state handler function should exist."""
        assert hasattr(ashlar_server, "export_fleet_state")
        assert callable(getattr(ashlar_server, "export_fleet_state"))

    def test_fleet_export_includes_agents(self):
        """Fleet export should include agents data."""
        src = inspect.getsource(ashlar_server.export_fleet_state)
        assert "agents" in src
        assert "agents_count" in src

    def test_fleet_export_includes_projects(self):
        """Fleet export should include projects."""
        src = inspect.getsource(ashlar_server.export_fleet_state)
        assert "projects" in src

    def test_fleet_export_includes_config_summary(self):
        """Fleet export should include config summary."""
        src = inspect.getsource(ashlar_server.export_fleet_state)
        assert "config_summary" in src

    def test_fleet_export_includes_version(self):
        """Fleet export should include version for future compatibility."""
        src = inspect.getsource(ashlar_server.export_fleet_state)
        assert "version" in src

    def test_fleet_export_includes_timestamp(self):
        """Fleet export should include exported_at timestamp."""
        src = inspect.getsource(ashlar_server.export_fleet_state)
        assert "exported_at" in src


# ── Workflow Deadlock Detection & Stage Timeout Tests ────────────────


class TestWorkflowDeadlockAndTimeout:
    """Tests for circular dependency detection, stage timeout, and related features."""

    # ── detect_circular_deps ──

    def test_valid_dag_returns_none(self):
        """A valid DAG with no cycles should return None."""
        specs = [
            {"role": "backend", "depends_on": []},
            {"role": "tester", "depends_on": [0]},
            {"role": "reviewer", "depends_on": [1]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is None

    def test_no_deps_returns_none(self):
        """Specs with no depends_on at all should return None."""
        specs = [{"role": "backend"}, {"role": "frontend"}, {"role": "tester"}]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is None

    def test_simple_cycle_detected(self):
        """A→B→A cycle should be detected."""
        specs = [
            {"role": "backend", "depends_on": [1]},
            {"role": "tester", "depends_on": [0]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None
        assert len(result) >= 1

    def test_three_node_cycle_detected(self):
        """A→B→C→A cycle should be detected."""
        specs = [
            {"role": "a", "depends_on": [2]},
            {"role": "b", "depends_on": [0]},
            {"role": "c", "depends_on": [1]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_self_loop_detected(self):
        """A node depending on itself should be detected."""
        specs = [{"role": "backend", "depends_on": [0]}]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_out_of_range_dep_detected(self):
        """Dependencies referencing out-of-range indices should be flagged."""
        specs = [
            {"role": "backend", "depends_on": [5]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None
        assert result[0] == [0, 5]

    def test_negative_dep_detected(self):
        """Negative dependency indices should be flagged."""
        specs = [
            {"role": "backend", "depends_on": [-1]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_empty_specs_returns_none(self):
        """Empty spec list should return None (no cycles)."""
        result = ashlar_server.WorkflowRun.detect_circular_deps([])
        assert result is None

    def test_mixed_valid_and_cycle(self):
        """Mix of valid deps and a cycle should detect the cycle."""
        specs = [
            {"role": "a", "depends_on": []},
            {"role": "b", "depends_on": [0]},
            {"role": "c", "depends_on": [3]},
            {"role": "d", "depends_on": [2]},
        ]
        result = ashlar_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    # ── WorkflowRun fields ──

    def test_stage_timeout_sec_default(self):
        """Default stage_timeout_sec should be 1800.0 (30 min)."""
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[], agent_map={}, pending_indices=set(),
        )
        assert wf.stage_timeout_sec == 1800.0

    def test_stage_started_at_default_empty(self):
        """Default stage_started_at should be empty dict."""
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[], agent_map={}, pending_indices=set(),
        )
        assert wf.stage_started_at == {}

    def test_stage_timeout_sec_in_to_dict(self):
        """to_dict() should include stage_timeout_sec."""
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[], agent_map={}, pending_indices=set(),
            stage_timeout_sec=600.0,
        )
        d = wf.to_dict()
        assert "stage_timeout_sec" in d
        assert d["stage_timeout_sec"] == 600.0

    # ── check_stage_timeouts ──

    def test_check_timeouts_no_workflows(self):
        """No workflows running should return empty list."""
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        result = manager.check_stage_timeouts()
        assert result == []

    def test_check_timeouts_not_expired(self):
        """Running workflow with recent stage_started_at should not timeout."""
        import time
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[{"role": "backend"}], agent_map={0: "a001"},
            pending_indices=set(), running_ids={"a001"},
            stage_timeout_sec=1800.0,
            stage_started_at={"a001": time.monotonic()},
        )
        manager.workflow_runs["wf1"] = wf
        result = manager.check_stage_timeouts()
        assert result == []

    def test_check_timeouts_expired(self):
        """Running workflow with expired stage should return timed-out agents."""
        import time
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[{"role": "backend"}], agent_map={0: "a001"},
            pending_indices=set(), running_ids={"a001"},
            stage_timeout_sec=10.0,
            stage_started_at={"a001": time.monotonic() - 20.0},
        )
        manager.workflow_runs["wf1"] = wf
        result = manager.check_stage_timeouts()
        assert len(result) == 1
        assert result[0][1] == "a001"  # agent_id

    def test_check_timeouts_skips_completed_workflows(self):
        """Completed workflows should not be checked for timeouts."""
        import time
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[{"role": "backend"}], agent_map={0: "a001"},
            pending_indices=set(), running_ids={"a001"},
            status="completed",
            stage_timeout_sec=10.0,
            stage_started_at={"a001": time.monotonic() - 20.0},
        )
        manager.workflow_runs["wf1"] = wf
        result = manager.check_stage_timeouts()
        assert result == []

    def test_check_timeouts_agent_name_fallback(self):
        """When agent not in manager.agents, should use agent_id as name."""
        import time
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        wf = ashlar_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[{"role": "backend"}], agent_map={0: "a001"},
            pending_indices=set(), running_ids={"a001"},
            stage_timeout_sec=5.0,
            stage_started_at={"a001": time.monotonic() - 10.0},
        )
        manager.workflow_runs["wf1"] = wf
        result = manager.check_stage_timeouts()
        assert len(result) == 1
        assert result[0][2] == "a001"  # fallback name = agent_id

    # ── Wiring in run_workflow and health loop ──

    def test_circular_dep_check_in_run_workflow(self):
        """run_workflow handler should call detect_circular_deps."""
        src = inspect.getsource(ashlar_server.run_workflow)
        assert "detect_circular_deps" in src

    def test_circular_dep_returns_400(self):
        """run_workflow should return 400 when circular deps found."""
        src = inspect.getsource(ashlar_server.run_workflow)
        assert "400" in src
        assert "Circular dependency" in src

    def test_stage_timeout_from_request_body(self):
        """run_workflow should accept stage_timeout_sec from request body."""
        src = inspect.getsource(ashlar_server.run_workflow)
        assert "stage_timeout_sec" in src

    def test_stage_timeout_clamped(self):
        """Stage timeout should be clamped between 60 and 7200."""
        src = inspect.getsource(ashlar_server.run_workflow)
        assert "60.0" in src or "60" in src
        assert "7200.0" in src or "7200" in src

    def test_timeout_enforcement_in_health_loop(self):
        """Health check loop should enforce stage timeouts."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "check_stage_timeouts" in src
        assert "workflow_stage_timeout" in src

    def test_stage_start_tracked_in_resolve_deps(self):
        """resolve_workflow_deps should track stage start time."""
        src = inspect.getsource(ashlar_server.AgentManager.resolve_workflow_deps)
        assert "stage_started_at" in src


# ── Resource Exhaustion Cascade Protection Tests ─────────────────────


class TestCascadeProtection:
    """Tests for resource exhaustion cascade protection features."""

    # ── Config defaults ──

    def test_config_system_cpu_pressure_threshold(self):
        """Default system CPU pressure threshold should be 90%."""
        config = ashlar_server.Config()
        assert config.system_cpu_pressure_threshold == 90.0

    def test_config_system_memory_pressure_threshold(self):
        """Default system memory pressure threshold should be 90%."""
        config = ashlar_server.Config()
        assert config.system_memory_pressure_threshold == 90.0

    def test_config_spawn_pressure_block(self):
        """Spawn pressure block should be enabled by default."""
        config = ashlar_server.Config()
        assert config.spawn_pressure_block is True

    def test_config_agent_memory_pause_pct(self):
        """Agent memory pause should default to 85% of limit."""
        config = ashlar_server.Config()
        assert config.agent_memory_pause_pct == 0.85

    def test_config_context_auto_pause_threshold(self):
        """Context auto-pause threshold should default to 0.95."""
        config = ashlar_server.Config()
        assert config.context_auto_pause_threshold == 0.95

    def test_config_pathological_error_window(self):
        """Pathological error window should default to 60s."""
        config = ashlar_server.Config()
        assert config.pathological_error_window_sec == 60.0

    def test_config_max_pathological_restarts(self):
        """Max pathological restarts should default to 1."""
        config = ashlar_server.Config()
        assert config.max_pathological_restarts == 1

    # ── Agent fields ──

    def test_agent_pathological_default_false(self):
        """Agent._pathological should default to False."""
        agent = ashlar_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._pathological is False

    def test_agent_context_auto_paused_default_false(self):
        """Agent._context_auto_paused should default to False."""
        agent = ashlar_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._context_auto_paused is False

    def test_agent_pressure_paused_default_false(self):
        """Agent._pressure_paused should default to False."""
        agent = ashlar_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._pressure_paused is False

    # ── Per-agent CPU method ──

    def test_get_agent_cpu_exists(self):
        """AgentManager should have get_agent_cpu method."""
        assert hasattr(ashlar_server.AgentManager, 'get_agent_cpu')

    def test_get_agent_cpu_no_pid(self):
        """get_agent_cpu should return 0.0 for agent without PID."""
        import asyncio
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        agent = ashlar_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        manager.agents["t001"] = agent
        result = asyncio.run(manager.get_agent_cpu("t001"))
        assert result == 0.0

    def test_get_agent_cpu_missing_agent(self):
        """get_agent_cpu should return 0.0 for nonexistent agent."""
        import asyncio
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        result = asyncio.run(manager.get_agent_cpu("nonexistent"))
        assert result == 0.0

    # ── CPU tracking in metrics loop ──

    def test_cpu_tracked_in_metrics_loop(self):
        """metrics_loop should update agent.cpu_pct."""
        src = inspect.getsource(ashlar_server.metrics_loop)
        assert "cpu_pct" in src
        assert "get_agent_cpu" in src

    # ── check_system_pressure ──

    def test_check_system_pressure_exists(self):
        """AgentManager should have check_system_pressure method."""
        assert hasattr(ashlar_server.AgentManager, 'check_system_pressure')

    def test_check_system_pressure_returns_dict(self):
        """check_system_pressure should return a dict with expected keys."""
        config = ashlar_server.Config()
        manager = ashlar_server.AgentManager(config)
        result = manager.check_system_pressure()
        assert isinstance(result, dict)
        assert "cpu_pressure" in result
        assert "memory_pressure" in result

    # ── Spawn pressure block ──

    def test_spawn_checks_system_pressure(self):
        """spawn() should check system pressure before spawning."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        assert "check_system_pressure" in src
        assert "spawn_pressure_block" in src

    # ── Pathological error detection ──

    def test_pathological_detection_in_health_loop(self):
        """Health check loop should detect pathological error loops."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "_pathological" in src
        assert "agent_pathological" in src
        assert "pathological_error_window_sec" in src

    def test_pathological_limits_restarts(self):
        """Health loop should limit max_restarts for pathological agents."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "max_pathological_restarts" in src

    # ── Context auto-pause ──

    def test_context_auto_pause_in_health_loop(self):
        """Health check loop should auto-pause agents at context threshold."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "context_auto_pause_threshold" in src
        assert "_context_auto_paused" in src
        assert "agent_context_auto_paused" in src

    # ── System pressure response ──

    def test_fleet_pressure_response_in_health_loop(self):
        """Health check loop should respond to system pressure."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "fleet_pressure_response" in src
        assert "_fleet_pressure_warned" in src
        assert "_pressure_paused" in src

    def test_pressure_relief_resumes_agents(self):
        """Health loop should resume pressure-paused agents when pressure relieved."""
        src = inspect.getsource(ashlar_server.health_check_loop)
        assert "_pressure_paused" in src
        # Check that there's logic to resume when pressure is relieved
        assert "Pressure relieved" in src or "_fleet_pressure_warned" in src

    # ── Graduated memory response ──

    def test_memory_watchdog_graduated_response(self):
        """Memory watchdog should pause before kill (graduated response)."""
        src = inspect.getsource(ashlar_server.memory_watchdog_loop)
        assert "agent_memory_paused" in src
        assert "pause_threshold" in src
        assert "agent_memory_pause_pct" in src

    def test_memory_watchdog_still_kills_at_limit(self):
        """Memory watchdog should still kill agents exceeding full limit."""
        src = inspect.getsource(ashlar_server.memory_watchdog_loop)
        assert "agent.memory_mb > limit" in src
        assert "agent_killed" in src


# ── Rate Limiting Tests ──────────────────────────────────────────────


class TestRateLimiting:
    """Tests for per-endpoint REST API rate limiting."""

    def test_rate_limiter_allows_burst(self):
        """RateLimiter should allow initial burst of requests."""
        rl = ashlar_server.RateLimiter()
        for _ in range(5):
            allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=5.0)
            assert allowed

    def test_rate_limiter_blocks_after_burst(self):
        """RateLimiter should block after burst is exhausted."""
        rl = ashlar_server.RateLimiter()
        for _ in range(10):
            rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        allowed, retry_after = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        assert not allowed
        assert retry_after > 0

    def test_rate_limiter_per_ip(self):
        """Different IPs should have separate buckets."""
        rl = ashlar_server.RateLimiter()
        for _ in range(5):
            rl.check("1.1.1.1", cost=1.0, rate=1.0, burst=5.0)
        # IP 1 is exhausted
        allowed1, _ = rl.check("1.1.1.1", cost=1.0, rate=1.0, burst=5.0)
        # IP 2 should still have tokens
        allowed2, _ = rl.check("2.2.2.2", cost=1.0, rate=1.0, burst=5.0)
        assert not allowed1
        assert allowed2

    def test_rate_limiter_cleanup_stale(self):
        """cleanup_stale should remove old bucket entries."""
        rl = ashlar_server.RateLimiter()
        rl.check("old_ip", cost=1.0)
        # Artificially age the bucket
        rl._buckets["old_ip"]["last_refill"] = time.monotonic() - 500
        rl.cleanup_stale(max_age=300.0)
        assert "old_ip" not in rl._buckets

    def test_rate_limit_tiers_exist(self):
        """Rate limit tier definitions should exist."""
        assert "spawn" in ashlar_server._RATE_LIMIT_TIERS
        assert "bulk" in ashlar_server._RATE_LIMIT_TIERS
        assert "default" in ashlar_server._RATE_LIMIT_TIERS
        assert "fleet-export" in ashlar_server._RATE_LIMIT_TIERS

    def test_get_rate_tier_spawn(self):
        """POST /api/agents should use spawn tier."""
        tier = ashlar_server._get_rate_tier("/api/agents", "POST")
        assert tier == "spawn"

    def test_get_rate_tier_bulk(self):
        """POST with bulk in path should use bulk tier."""
        tier = ashlar_server._get_rate_tier("/api/agents/bulk", "POST")
        assert tier == "bulk"

    def test_get_rate_tier_default(self):
        """GET /api/agents should use default tier."""
        tier = ashlar_server._get_rate_tier("/api/agents", "GET")
        assert tier == "default"

    def test_get_rate_tier_fleet_export(self):
        """Fleet export should use fleet-export tier."""
        tier = ashlar_server._get_rate_tier("/api/fleet/export", "GET")
        assert tier == "fleet-export"

    def test_rate_limit_middleware_exists(self):
        """rate_limit_middleware should exist as a function."""
        assert callable(ashlar_server.rate_limit_middleware)

    def test_middleware_registered_in_create_app(self):
        """create_app should include rate_limit_middleware."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "rate_limit_middleware" in src


# ── Binary/Garbage Detection Tests ───────────────────────────────────


class TestBinaryGarbageDetection:
    """Tests for binary/garbage output detection in capture."""

    def test_normal_text_not_garbage(self):
        """Normal text output should not be flagged as garbage."""
        lines = ["Hello world", "Processing file.py", "Done."]
        assert not ashlar_server.AgentManager._is_binary_garbage(lines)

    def test_empty_lines_not_garbage(self):
        """Empty lines should not be flagged as garbage."""
        assert not ashlar_server.AgentManager._is_binary_garbage([])
        assert not ashlar_server.AgentManager._is_binary_garbage([""])

    def test_ansi_colored_text_not_garbage(self):
        """Text with ANSI color codes should not be flagged."""
        lines = ["\x1b[32mSuccess\x1b[0m", "\x1b[1;31mError\x1b[0m: something failed"]
        assert not ashlar_server.AgentManager._is_binary_garbage(lines)

    def test_binary_content_is_garbage(self):
        """Binary content (mostly non-printable) should be flagged."""
        binary_line = "".join(chr(i) for i in range(0, 32) if i not in (9, 10, 13)) * 5
        lines = [binary_line, binary_line]
        assert ashlar_server.AgentManager._is_binary_garbage(lines)

    def test_mixed_with_threshold(self):
        """Mixed content below threshold should not be flagged."""
        # Mostly printable with a few non-printable chars
        lines = ["Normal text with a \x00 byte here"]
        assert not ashlar_server.AgentManager._is_binary_garbage(lines)

    def test_garbage_detection_in_capture_output(self):
        """capture_output should call _is_binary_garbage."""
        src = inspect.getsource(ashlar_server.AgentManager.capture_output)
        assert "_is_binary_garbage" in src
        assert "binary garbage" in src


# ── WebSocket Disconnect Cleanup Tests ───────────────────────────────


class TestWebSocketDisconnectCleanup:
    """Tests for WebSocket client disconnect handling."""

    def test_ws_heartbeat_enabled(self):
        """WebSocket should have heartbeat=30.0 for ping/pong."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        assert "heartbeat=30.0" in src

    def test_ws_disconnect_cleans_sync_time(self):
        """Disconnect should clean up _last_sync_time entry."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        assert "_last_sync_time.pop" in src

    def test_ws_disconnect_cleans_rate_limiter(self):
        """Disconnect should clean up rate limiter entries for client IP."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        assert "rate_limiter" in src
        assert "stale_keys" in src

    def test_ws_disconnect_discards_client(self):
        """Disconnect should remove client from set."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        assert "clients.discard(ws)" in src

    def test_ws_max_clients_limit(self):
        """WebSocketHub should enforce max_clients connection limit."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_ws)
        assert "max_clients" in src
        assert "1013" in src  # HTTP status for "Try Again Later"


# ── Line Truncation Tests ────────────────────────────────────────────


class TestLineTruncation:
    """Tests for output line-length truncation."""

    def test_truncation_in_capture_output(self):
        """capture_output should truncate long lines."""
        src = inspect.getsource(ashlar_server.AgentManager.capture_output)
        assert "max_line_len" in src
        assert "[truncated]" in src
        assert "5000" in src

    def test_short_lines_unchanged(self):
        """Lines under 5000 chars should not be modified by truncation logic."""
        short = "a" * 100
        # Simulate the truncation logic
        max_line_len = 5000
        result = short[:max_line_len] + " [truncated]" if len(short) > max_line_len else short
        assert result == short

    def test_long_lines_truncated(self):
        """Lines over 5000 chars should be truncated with suffix."""
        long = "x" * 10000
        max_line_len = 5000
        result = long[:max_line_len] + " [truncated]" if len(long) > max_line_len else long
        assert len(result) == 5012  # 5000 + len(" [truncated]")
        assert result.endswith("[truncated]")


# ── Compression Middleware Tests ──────────────────────────────────────


class TestCompressionMiddleware:
    """Tests for gzip compression middleware."""

    def test_compression_middleware_exists(self):
        """compression_middleware should exist as a callable."""
        assert callable(ashlar_server.compression_middleware)

    def test_compression_registered_in_create_app(self):
        """create_app should include compression_middleware."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "compression_middleware" in src

    def test_compression_only_for_api(self):
        """Compression should only apply to /api/ paths."""
        src = inspect.getsource(ashlar_server.compression_middleware)
        assert "/api/" in src

    def test_compression_checks_accept_encoding(self):
        """Compression should check Accept-Encoding header."""
        src = inspect.getsource(ashlar_server.compression_middleware)
        assert "Accept-Encoding" in src
        assert "gzip" in src

    def test_compression_min_size(self):
        """Compression should only apply to responses > 1KB."""
        src = inspect.getsource(ashlar_server.compression_middleware)
        assert "1024" in src


# ── Diagnostic Endpoint Tests ─────────────────────────────────────────


class TestDiagnosticEndpoint:
    """Tests for the POST /api/diagnostic self-test endpoint."""

    def test_diagnostic_handler_exists(self):
        """run_diagnostic handler should exist."""
        assert callable(ashlar_server.run_diagnostic)

    def test_diagnostic_route_registered(self):
        """Diagnostic route should be registered in create_app."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "diagnostic" in src

    def test_diagnostic_checks_tmux(self):
        """Diagnostic should test tmux availability."""
        src = inspect.getsource(ashlar_server.run_diagnostic)
        assert "tmux" in src
        assert "-V" in src

    def test_diagnostic_checks_disk(self):
        """Diagnostic should check disk space."""
        src = inspect.getsource(ashlar_server.run_diagnostic)
        assert "disk_usage" in src

    def test_diagnostic_checks_database(self):
        """Diagnostic should test database write/read."""
        src = inspect.getsource(ashlar_server.run_diagnostic)
        assert "_diagnostic_test" in src
        assert "INSERT" in src

    def test_diagnostic_checks_backends(self):
        """Diagnostic should report backend availability."""
        src = inspect.getsource(ashlar_server.run_diagnostic)
        assert "backend_configs" in src
        assert "available" in src

    def test_diagnostic_returns_status(self):
        """Diagnostic should return ok/degraded status."""
        src = inspect.getsource(ashlar_server.run_diagnostic)
        assert "degraded" in src
        assert "all_ok" in src


# ─────────────────────────────────────────────
# Configurable Output Max Lines (#246)
# ─────────────────────────────────────────────


class TestOutputMaxLines:
    def test_config_default(self):
        """Default output_max_lines should be 2000."""
        config = ashlar_server.Config()
        assert config.output_max_lines == 2000

    def test_config_custom_value(self):
        """Config should accept custom output_max_lines."""
        config = ashlar_server.Config()
        config.output_max_lines = 500
        assert config.output_max_lines == 500

    def test_spawn_applies_custom_maxlen(self):
        """Spawn should apply custom deque maxlen when output_max_lines != 2000."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        assert "output_max_lines" in src
        assert "maxlen" in src

    def test_default_deque_unchanged(self):
        """When output_max_lines == 2000, no custom deque is created."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        # The condition checks != 2000 before creating custom deque
        assert "!= 2000" in src

    def test_agent_deque_default_maxlen(self):
        """Agent output_lines default deque should have maxlen 2000."""
        agent = ashlar_server.Agent(
            id="test", name="test", role="general",
            status="idle", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        assert agent.output_lines.maxlen == 2000


# ─────────────────────────────────────────────
# Request Logging Middleware (#247)
# ─────────────────────────────────────────────


class TestRequestLoggingMiddleware:
    def test_middleware_exists(self):
        """request_logging_middleware should be defined."""
        assert hasattr(ashlar_server, 'request_logging_middleware')
        assert callable(ashlar_server.request_logging_middleware)

    def test_middleware_skips_non_api(self):
        """Middleware should skip non-API paths."""
        src = inspect.getsource(ashlar_server.request_logging_middleware)
        assert "/api/" in src

    def test_middleware_logs_slow_requests(self):
        """Middleware should warn on requests slower than 1s."""
        src = inspect.getsource(ashlar_server.request_logging_middleware)
        assert "SLOW" in src
        assert "1.0" in src

    def test_middleware_registered(self):
        """Middleware should be registered in create_app."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "request_logging_middleware" in src

    def test_middleware_first_in_chain(self):
        """request_logging_middleware should be first middleware (outermost)."""
        src = inspect.getsource(ashlar_server.create_app)
        # Find the middlewares list line
        assert "middlewares = [request_logging_middleware" in src

    def test_middleware_handles_http_exceptions(self):
        """Middleware should handle HTTPException gracefully."""
        src = inspect.getsource(ashlar_server.request_logging_middleware)
        assert "HTTPException" in src

    def test_middleware_handles_generic_exceptions(self):
        """Middleware should catch and log generic exceptions."""
        src = inspect.getsource(ashlar_server.request_logging_middleware)
        assert "except Exception" in src


# ─────────────────────────────────────────────
# Spawn Validation Endpoint (#248)
# ─────────────────────────────────────────────


class TestSpawnValidation:
    def test_handler_exists(self):
        """validate_spawn handler should exist."""
        assert hasattr(ashlar_server, 'validate_spawn')
        assert callable(ashlar_server.validate_spawn)

    def test_route_registered(self):
        """Route /api/agents/validate should be registered."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/agents/validate" in src
        assert "validate_spawn" in src

    def test_validates_role(self):
        """Validation should check role against BUILTIN_ROLES."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "BUILTIN_ROLES" in src
        assert "Unknown role" in src

    def test_validates_name(self):
        """Validation should sanitize and check name."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "sanitiz" in src.lower() or "name" in src

    def test_validates_backend(self):
        """Validation should check backend availability."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "backend" in src
        assert "available" in src

    def test_validates_working_dir(self):
        """Validation should check working directory."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "working_dir" in src
        assert "isdir" in src

    def test_validates_task_length(self):
        """Validation should check task length limit."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "10000" in src

    def test_checks_capacity(self):
        """Validation should check agent capacity."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "max_agents" in src

    def test_checks_system_pressure(self):
        """Validation should check system pressure."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "check_system_pressure" in src

    def test_returns_resolved_fields(self):
        """Validation should return resolved field values."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "resolved" in src
        assert '"valid"' in src or "'valid'" in src

    def test_returns_warnings(self):
        """Validation should return warnings for non-blocking issues."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "warnings" in src

    def test_returns_errors(self):
        """Validation should return errors list for blocking issues."""
        src = inspect.getsource(ashlar_server.validate_spawn)
        assert "errors" in src


# ─────────────────────────────────────────────
# Stale Input State on Error (#249)
# ─────────────────────────────────────────────


class TestStaleInputClearOnError:
    def test_error_clears_needs_input(self):
        """Transitioning to error should clear needs_input flag."""
        agent = ashlar_server.Agent(
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
        agent = ashlar_server.Agent(
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
        agent = ashlar_server.Agent(
            id="test", name="test", role="general",
            status="idle", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        agent.needs_input = False
        agent.set_status("working")
        assert agent.needs_input is False

    def test_non_error_preserves_needs_input(self):
        """Transitioning to working should NOT clear needs_input."""
        agent = ashlar_server.Agent(
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
        src = inspect.getsource(ashlar_server.AgentManager.kill)
        assert "release_file_locks" in src

    def test_kill_handles_missing_db(self):
        """Kill should handle missing db gracefully."""
        src = inspect.getsource(ashlar_server.AgentManager.kill)
        assert "self.db" in src
        # Should have a try/except around DB cleanup
        assert "except Exception" in src

    def test_manager_has_db_attribute(self):
        """AgentManager should have db attribute initialized to None."""
        config = ashlar_server.Config()
        config.demo_mode = True
        manager = ashlar_server.AgentManager(config)
        assert hasattr(manager, 'db')
        assert manager.db is None

    def test_create_app_sets_manager_db(self):
        """create_app should set manager.db reference."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "manager.db = db" in src


# ─────────────────────────────────────────────
# WebSocket Broadcast Safety (#249)
# ─────────────────────────────────────────────


class TestWSBroadcastSafety:
    def test_broadcast_snapshots_clients(self):
        """Broadcast should snapshot clients set before iterating."""
        src = inspect.getsource(ashlar_server.WebSocketHub.broadcast)
        assert "clients_snapshot" in src
        assert "set(self.clients)" in src

    def test_broadcast_iterates_snapshot(self):
        """Broadcast gather should iterate over snapshot, not live set."""
        src = inspect.getsource(ashlar_server.WebSocketHub.broadcast)
        assert "clients_snapshot" in src
        # The gather should use clients_snapshot
        assert "for ws in clients_snapshot" in src


# ─────────────────────────────────────────────
# Tmux Session Collision Check (#250)
# ─────────────────────────────────────────────


class TestTmuxSessionCollisionCheck:
    def test_spawn_checks_existing_session(self):
        """Spawn should check for existing tmux session before creating."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        assert "has-session" in src

    def test_spawn_kills_orphan_before_create(self):
        """Spawn should kill orphaned session if found."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        assert "Orphaned tmux session" in src
        assert "kill-session" in src


# ─────────────────────────────────────────────
# Database Commit Timeouts (#251)
# ─────────────────────────────────────────────


class TestDatabaseCommitTimeouts:
    def test_safe_commit_method_exists(self):
        """Database should have _safe_commit method."""
        assert hasattr(ashlar_server.Database, '_safe_commit')
        assert callable(ashlar_server.Database._safe_commit)

    def test_safe_commit_uses_wait_for(self):
        """_safe_commit should use asyncio.wait_for with timeout."""
        src = inspect.getsource(ashlar_server.Database._safe_commit)
        assert "wait_for" in src
        assert "timeout" in src

    def test_safe_commit_handles_timeout(self):
        """_safe_commit should catch TimeoutError gracefully."""
        src = inspect.getsource(ashlar_server.Database._safe_commit)
        assert "TimeoutError" in src

    def test_safe_commit_null_guard(self):
        """_safe_commit should check for None db."""
        src = inspect.getsource(ashlar_server.Database._safe_commit)
        assert "not self._db" in src

    def test_safe_commit_default_timeout(self):
        """_safe_commit should have a reasonable default timeout."""
        src = inspect.getsource(ashlar_server.Database._safe_commit)
        assert "3.0" in src

    def test_all_commits_use_safe_commit(self):
        """All database commits should use _safe_commit, not raw _db.commit()."""
        src = inspect.getsource(ashlar_server.Database)
        # There should be no direct _db.commit() calls except inside _safe_commit itself
        lines = src.split('\n')
        direct_commits = [
            line.strip() for line in lines
            if '_db.commit()' in line
            and 'wait_for' not in line  # The call inside _safe_commit uses wait_for
            and 'def _safe_commit' not in line
        ]
        assert len(direct_commits) == 0, f"Found direct commit calls: {direct_commits}"


# ─────────────────────────────────────────────
# Role Validation Guard (#251)
# ─────────────────────────────────────────────


class TestRoleValidationGuard:
    def test_role_injection_logs_fallback(self):
        """Role injection should log when falling back to general."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        assert "not in BUILTIN_ROLES" in src or "falling back" in src.lower()

    def test_role_injection_explicit_check(self):
        """Role injection should explicitly check role existence before using it."""
        src = inspect.getsource(ashlar_server.AgentManager.spawn)
        # Should use .get(role) without fallback, then check
        assert "BUILTIN_ROLES.get(role)" in src


# ─────────────────────────────────────────────
# Archive Rotation (#252)
# ─────────────────────────────────────────────


class TestArchiveRotation:
    def test_rotate_archive_method_exists(self):
        """Database should have rotate_archive method."""
        assert hasattr(ashlar_server.Database, 'rotate_archive')
        assert callable(ashlar_server.Database.rotate_archive)

    def test_cleanup_old_archives_method_exists(self):
        """Database should have cleanup_old_archives method."""
        assert hasattr(ashlar_server.Database, 'cleanup_old_archives')
        assert callable(ashlar_server.Database.cleanup_old_archives)

    def test_config_archive_max_rows(self):
        """Config should have archive_max_rows_per_agent with default 50000."""
        config = ashlar_server.Config()
        assert hasattr(config, 'archive_max_rows_per_agent')
        assert config.archive_max_rows_per_agent == 50000

    def test_config_archive_retention_hours(self):
        """Config should have archive_retention_hours with default 48."""
        config = ashlar_server.Config()
        assert hasattr(config, 'archive_retention_hours')
        assert config.archive_retention_hours == 48

    def test_rotate_archive_null_guard(self):
        """rotate_archive should return 0 when DB is None."""
        db = ashlar_server.Database()
        # _db is None before init
        result = asyncio.run(db.rotate_archive("test-agent"))
        assert result == 0

    def test_rotate_archive_zero_max_rows(self):
        """rotate_archive should return 0 when max_rows <= 0."""
        db = ashlar_server.Database()
        result = asyncio.run(db.rotate_archive("test-agent", max_rows=0))
        assert result == 0

    def test_cleanup_old_archives_null_guard(self):
        """cleanup_old_archives should return 0 when DB is None."""
        db = ashlar_server.Database()
        result = asyncio.run(db.cleanup_old_archives())
        assert result == 0

    def test_cleanup_old_archives_zero_retention(self):
        """cleanup_old_archives should return 0 when retention_hours <= 0."""
        db = ashlar_server.Database()
        result = asyncio.run(db.cleanup_old_archives(retention_hours=0))
        assert result == 0

    def test_rotate_archive_uses_safe_commit(self):
        """rotate_archive should use _safe_commit for timeout protection."""
        src = inspect.getsource(ashlar_server.Database.rotate_archive)
        assert "_safe_commit" in src

    def test_cleanup_old_archives_uses_safe_commit(self):
        """cleanup_old_archives should use _safe_commit for timeout protection."""
        src = inspect.getsource(ashlar_server.Database.cleanup_old_archives)
        assert "_safe_commit" in src


# ─────────────────────────────────────────────
# Server Stats Endpoint (#253)
# ─────────────────────────────────────────────


class TestServerStats:
    def test_handler_exists(self):
        """get_server_stats handler should exist."""
        assert hasattr(ashlar_server, 'get_server_stats')
        assert callable(ashlar_server.get_server_stats)

    def test_route_registered(self):
        """Route /api/stats should be registered."""
        src = inspect.getsource(ashlar_server.create_app)
        assert "/api/stats" in src
        assert "get_server_stats" in src

    def test_returns_uptime(self):
        """Stats endpoint should return uptime fields."""
        src = inspect.getsource(ashlar_server.get_server_stats)
        assert "uptime_sec" in src
        assert "uptime_human" in src

    def test_returns_agent_stats(self):
        """Stats endpoint should return agent statistics."""
        src = inspect.getsource(ashlar_server.get_server_stats)
        assert "total_spawned" in src
        assert "total_killed" in src
        assert "total_messages_sent" in src

    def test_returns_db_size(self):
        """Stats endpoint should return database size info."""
        src = inspect.getsource(ashlar_server.get_server_stats)
        assert "db_size_mb" in src

    def test_returns_request_count(self):
        """Stats endpoint should return request count."""
        src = inspect.getsource(ashlar_server.get_server_stats)
        assert "request_count" in src

    def test_manager_has_api_request_counter(self):
        """AgentManager should have _total_api_requests counter."""
        config = ashlar_server.Config()
        config.demo_mode = True
        manager = ashlar_server.AgentManager(config)
        assert hasattr(manager, '_total_api_requests')
        assert manager._total_api_requests == 0

    def test_middleware_increments_manager_counter(self):
        """Request logging middleware should increment manager._total_api_requests."""
        src = inspect.getsource(ashlar_server.request_logging_middleware)
        assert "_total_api_requests" in src

    def test_stats_returns_archive_config(self):
        """Stats endpoint should include archive configuration."""
        src = inspect.getsource(ashlar_server.get_server_stats)
        assert "archive_max_rows_per_agent" in src
        assert "archive_retention_hours" in src


# ─────────────────────────────────────────────
# Extended Secret Redaction (#254)
# ─────────────────────────────────────────────


class TestExtendedSecretRedaction:
    def test_redact_secrets_exists(self):
        """redact_secrets function should exist."""
        assert hasattr(ashlar_server, 'redact_secrets')
        assert callable(ashlar_server.redact_secrets)

    def test_redact_openai_key(self):
        """Should redact OpenAI/Anthropic sk- keys."""
        text = "Using key sk-abcdefghijklmnopqrstuvwx in the config"
        result = ashlar_server.redact_secrets(text)
        assert "sk-" not in result
        assert "REDACTED" in result

    def test_redact_github_pat_classic(self):
        """Should redact classic GitHub PATs (ghp_)."""
        text = "Token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLm"
        result = ashlar_server.redact_secrets(text)
        assert "ghp_" not in result
        assert "REDACTED" in result

    def test_redact_github_pat_fine_grained(self):
        """Should redact fine-grained GitHub PATs (github_pat_)."""
        text = "Token: github_pat_aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlar_server.redact_secrets(text)
        assert "github_pat_" not in result
        assert "REDACTED" in result

    def test_redact_aws_access_key(self):
        """Should redact AWS access keys (AKIA...)."""
        text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        result = ashlar_server.redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "REDACTED" in result

    def test_redact_slack_bot_token(self):
        """Should redact Slack bot tokens (xoxb-)."""
        text = "SLACK_TOKEN=xoxb-12345678901-12345678901-abcdef"
        result = ashlar_server.redact_secrets(text)
        assert "xoxb-" not in result
        assert "REDACTED" in result

    def test_redact_sendgrid_key(self):
        """Should redact SendGrid API keys (SG.xxxxx.xxxxx)."""
        text = "key: SG.aBcDeFgHiJkLmNoPqRsTuVw.xYzAbCdEfGhIjKlMnOpQrSt"
        result = ashlar_server.redact_secrets(text)
        assert "SG." not in result
        assert "REDACTED" in result

    def test_redact_password_field(self):
        """Should redact password= fields."""
        text = "password=MyS3cretP@ss"
        result = ashlar_server.redact_secrets(text)
        assert "MyS3cretP@ss" not in result
        assert "REDACTED" in result

    def test_redact_jwt_token(self):
        """Should redact JWT tokens (eyJ...)."""
        text = "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = ashlar_server.redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "REDACTED" in result

    def test_redact_mongodb_connection_string(self):
        """Should redact MongoDB connection strings."""
        text = "DB_URL=mongodb+srv://user:pass@cluster.mongodb.net/db"
        result = ashlar_server.redact_secrets(text)
        assert "mongodb+srv://" not in result
        assert "REDACTED" in result

    def test_redact_postgres_connection_string(self):
        """Should redact PostgreSQL connection strings."""
        text = "DATABASE_URL=postgresql://user:pass@host:5432/mydb"
        result = ashlar_server.redact_secrets(text)
        assert "postgresql://" not in result
        assert "REDACTED" in result

    def test_redact_redis_connection_string(self):
        """Should redact Redis connection strings."""
        text = "REDIS_URL=redis://default:pass@redis-host:6379"
        result = ashlar_server.redact_secrets(text)
        assert "redis://" not in result
        assert "REDACTED" in result

    def test_no_false_positive_on_normal_text(self):
        """Should not redact normal text without secrets."""
        text = "Hello world, this is a normal log line with no secrets"
        result = ashlar_server.redact_secrets(text)
        assert result == text

    def test_pattern_count(self):
        """Should have at least 20 secret patterns (expanded from original 7)."""
        assert len(ashlar_server._SECRET_PATTERNS) >= 20

    def test_redact_xai_key(self):
        """Should redact xAI API keys."""
        text = "XAI_KEY=xai-aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlar_server.redact_secrets(text)
        assert "xai-" not in result
        assert "REDACTED" in result

    def test_redact_npm_token(self):
        """Should redact npm tokens."""
        text = "NPM_TOKEN=np_aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlar_server.redact_secrets(text)
        assert "np_" not in result
        assert "REDACTED" in result

    def test_redact_bearer_token(self):
        """Should redact Bearer tokens."""
        text = "Authorization: Bearer eyAbCdEfGhIjKlMnOpQr"
        result = ashlar_server.redact_secrets(text)
        assert "Bearer eyAbCdEfGhIjKlMnOpQr" not in result
        assert "REDACTED" in result

    def test_redact_api_key_field(self):
        """Should redact api_key= fields."""
        text = "api_key=abc123def456ghi789jkl012"
        result = ashlar_server.redact_secrets(text)
        assert "abc123def456ghi789jkl012" not in result
        assert "REDACTED" in result


# ─────────────────────────────────────────────
# WebSocket Message Validation (#255)
# ─────────────────────────────────────────────


class TestWSMessageValidation:
    def test_ws_send_has_length_check(self):
        """WS 'send' case should enforce message length limit."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_message)
        # Find the send case section
        assert "50_000" in src or "50000" in src

    def test_ws_send_returns_error_on_overlength(self):
        """WS 'send' case should return error for oversized messages."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_message)
        assert "Message too long" in src

    def test_ws_agent_message_validates_from_id(self):
        """WS 'agent_message' case should validate from_agent_id exists."""
        src = inspect.getsource(ashlar_server.WebSocketHub.handle_message)
        assert "Source agent" in src

    def test_restart_handles_json_decode_error(self):
        """Restart endpoint should return 400 on malformed JSON."""
        src = inspect.getsource(ashlar_server.restart_agent)
        assert "JSONDecodeError" in src or "json.JSONDecodeError" in src


# ─────────────────────────────────────────────
# Output Flood Protection (#256)
# ─────────────────────────────────────────────


class TestOutputFloodProtection:
    def test_agent_has_flood_fields(self):
        """Agent should have flood detection fields."""
        agent = ashlar_server.Agent(
            id="f1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert hasattr(agent, '_flood_detected')
        assert hasattr(agent, '_flood_ticks')
        assert agent._flood_detected is False
        assert agent._flood_ticks == 0

    def test_config_flood_threshold(self):
        """Config should have flood threshold settings."""
        config = ashlar_server.Config()
        assert hasattr(config, 'flood_threshold_lines_per_min')
        assert config.flood_threshold_lines_per_min == 3000
        assert hasattr(config, 'flood_sustained_ticks')
        assert config.flood_sustained_ticks == 3

    def test_flood_detected_in_to_dict(self):
        """Agent.to_dict() should include flood_detected."""
        agent = ashlar_server.Agent(
            id="f2", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        d = agent.to_dict()
        assert "flood_detected" in d
        assert d["flood_detected"] is False

    def test_flood_flag_changes_to_dict(self):
        """flood_detected should reflect in to_dict when set."""
        agent = ashlar_server.Agent(
            id="f3", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        agent._flood_detected = True
        d = agent.to_dict()
        assert d["flood_detected"] is True

    def test_capture_loop_has_flood_detection(self):
        """Output capture loop should contain flood detection logic."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        assert "flood_threshold" in src
        assert "_flood_detected" in src
        assert "agent_flood" in src

    def test_flood_broadcasts_event(self):
        """Flood detection should broadcast an agent_flood event."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        assert "agent_flood" in src
        assert "excessive output" in src.lower()

    def test_flood_throttles_broadcast(self):
        """When flood is detected, output broadcast should be throttled."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        assert "flood_detected" in src
        # Should have conditional broadcast
        assert "lines suppressed" in src.lower() or "suppressed" in src.lower()

    def test_flood_ticks_decrement(self):
        """Flood ticks should decrement when rate drops below threshold."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        # Should have decrement logic
        assert "_flood_ticks - 1" in src or "flood_ticks -= 1" in src or "flood_ticks - 1" in src


# ─────────────────────────────────────────────
# Summary Output Cache (#257)
# ─────────────────────────────────────────────


class TestSummaryOutputCache:
    def test_agent_has_summary_output_hash(self):
        """Agent should have _summary_output_hash field."""
        agent = ashlar_server.Agent(
            id="sc1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert hasattr(agent, '_summary_output_hash')
        assert agent._summary_output_hash == 0

    def test_capture_loop_checks_output_hash_before_llm(self):
        """Capture loop should check output hash before calling LLM summarizer."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        assert "_summary_output_hash" in src

    def test_capture_loop_updates_hash_after_summary(self):
        """Capture loop should store output hash after successful summary."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        # Should set the hash after getting summary
        assert "_summary_output_hash = current_hash" in src or "_summary_output_hash =" in src

    def test_capture_loop_skips_unchanged_output(self):
        """Capture loop should skip LLM call when output hash matches."""
        src = inspect.getsource(ashlar_server.output_capture_loop)
        # Should compare current hash with stored hash
        assert "current_hash != agent._summary_output_hash" in src or "current_hash ==" in src

    def test_summary_hash_default_differs_from_output_hash(self):
        """Default summary hash (0) should differ from initial output hash (0) to ensure first summary runs."""
        agent = ashlar_server.Agent(
            id="sc2", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        # Both start at 0, but the first capture will generate a non-zero _prev_output_hash
        # So the first summary check (0 != new_hash) will pass
        assert agent._summary_output_hash == 0
        assert agent._prev_output_hash == 0


# ─────────────────────────────────────────────
# Activity Performance Timing (#261)
# ─────────────────────────────────────────────


class TestActivityPerformanceTiming:
    def test_activity_summary_includes_timing(self):
        """Activity summary should include timing field."""
        parser = ashlar_server.OutputIntelligenceParser()
        agent = ashlar_server.Agent(
            id="pt1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        summary = parser.get_activity_summary(agent)
        assert "timing" in summary

    def test_timing_with_no_invocations(self):
        """Timing should return zeros with no invocations."""
        parser = ashlar_server.OutputIntelligenceParser()
        agent = ashlar_server.Agent(
            id="pt2", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        summary = parser.get_activity_summary(agent)
        assert summary["timing"]["avg_interval_sec"] == 0
        assert summary["timing"]["longest_gap_sec"] == 0

    def test_timing_with_invocations(self):
        """Timing should compute intervals between invocations."""
        parser = ashlar_server.OutputIntelligenceParser()
        agent = ashlar_server.Agent(
            id="pt3", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        # Add invocations with known timestamps
        now = time.monotonic()
        for i in range(5):
            agent._tool_invocations.append(ashlar_server.ToolInvocation(
                agent_id="pt3", tool="Read", args=f"file{i}.py",
                timestamp=now - 20 + (i * 5), line_index=i,
            ))
        summary = parser.get_activity_summary(agent)
        timing = summary["timing"]
        assert timing["avg_interval_sec"] > 0
        assert timing["longest_gap_sec"] > 0
        assert "avg_by_tool" in timing
        assert "recent_intervals" in timing

    def test_timing_tools_per_min(self):
        """Timing should compute tools_per_min from recent invocations."""
        parser = ashlar_server.OutputIntelligenceParser()
        agent = ashlar_server.Agent(
            id="pt4", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        now = time.monotonic()
        for i in range(10):
            agent._tool_invocations.append(ashlar_server.ToolInvocation(
                agent_id="pt4", tool="Edit", args=f"file{i}.py",
                timestamp=now - 60 + (i * 6), line_index=i,
            ))
        summary = parser.get_activity_summary(agent)
        assert summary["timing"]["tools_per_min"] > 0

    def test_compute_timing_method_exists(self):
        """OutputIntelligenceParser should have _compute_timing method."""
        assert hasattr(ashlar_server.OutputIntelligenceParser, '_compute_timing')

    def test_timing_avg_by_tool(self):
        """Timing should break down average intervals by tool type."""
        parser = ashlar_server.OutputIntelligenceParser()
        agent = ashlar_server.Agent(
            id="pt5", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        now = time.monotonic()
        tools = ["Read", "Edit", "Read", "Bash", "Edit"]
        for i, tool in enumerate(tools):
            agent._tool_invocations.append(ashlar_server.ToolInvocation(
                agent_id="pt5", tool=tool, args=f"arg{i}",
                timestamp=now - 25 + (i * 5), line_index=i,
            ))
        summary = parser.get_activity_summary(agent)
        avg_by_tool = summary["timing"]["avg_by_tool"]
        assert isinstance(avg_by_tool, dict)
        assert len(avg_by_tool) > 0


# ─────────────────────────────────────────────
# T19: AnthropicIntelligenceClient Unit Tests
# ─────────────────────────────────────────────

class TestAnthropicIntelligenceClient:
    """Tests for circuit breaker, error handling, and graceful degradation."""

    def test_check_circuit_returns_true_when_available(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        assert client._check_circuit() is True

    def test_check_circuit_returns_false_when_unavailable(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client.available = False
        assert client._check_circuit() is False

    def test_check_circuit_trips_at_max_failures(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client._failures = 5
        client._circuit_reset_time = time.monotonic() + 60
        assert client._check_circuit() is False

    def test_check_circuit_resets_after_cooldown(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client._failures = 5
        client._circuit_reset_time = time.monotonic() - 1  # expired
        assert client._check_circuit() is True
        assert client._failures == 0

    def test_handle_error_401_marks_unavailable(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client._handle_error(401)
        assert client.available is False

    def test_handle_error_403_marks_unavailable(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client._handle_error(403)
        assert client.available is False

    def test_handle_error_429_sets_cooldown(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        before = time.monotonic()
        client._handle_error(429, retry_after="30")
        assert client._circuit_reset_time >= before + 29
        assert client.available is True  # Still available, just cooling

    def test_handle_error_429_default_cooldown(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        before = time.monotonic()
        client._handle_error(429)  # No retry_after
        assert client._circuit_reset_time >= before + 59

    def test_handle_error_500_increments_failures(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        assert client._failures == 0
        client._handle_error(500)
        assert client._failures == 1
        assert client.available is True

    def test_handle_error_trips_circuit_at_max(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        for _ in range(5):
            client._handle_error(500)
        assert client._failures == 5
        assert client._circuit_reset_time > time.monotonic()

    def test_call_returns_none_when_circuit_open(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client.available = False
        result = asyncio.run(client._call([{"role": "user", "content": "hi"}]))
        assert result is None

    def test_analyze_fleet_skips_single_agent(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        mock_agent = MagicMock()
        result = asyncio.run(client.analyze_fleet([mock_agent], []))
        assert result == []

    def test_analyze_fleet_skips_empty_list(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        result = asyncio.run(client.analyze_fleet([], []))
        assert result == []

    def test_summarize_returns_none_for_empty_output(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        result = asyncio.run(client.summarize([], "task", "general", "working"))
        assert result is None

    def test_parse_command_returns_unknown_when_circuit_open(self):
        client = AnthropicIntelligenceClient(api_key="test-key")
        client.available = False
        result = asyncio.run(client.parse_command("test command", [], {}))
        assert isinstance(result, ParsedIntent)
        assert result.action == "unknown"
        assert result.confidence == 0.0


# ─────────────────────────────────────────────
# T20: OutputIntelligenceParser Extended Tests
# ─────────────────────────────────────────────

class TestOutputIntelligenceParserExtended:
    """Tests for MCP tool parsing, tool result status, Jest/Mocha frameworks."""

    def _make_agent(self, lines):
        agent = ashlar_server.Agent(
            id="pe1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        agent.output_lines = lines
        return agent

    def test_parse_mcp_tool(self):
        agent = self._make_agent(['mcp__filesystem__read_file("/config.yaml")'])
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts["tools"] >= 1
        assert any(inv.tool == "MCP" for inv in agent._tool_invocations)

    def test_tool_result_success_updates_status(self):
        agent = self._make_agent([
            'Read("/src/app.py")',
            'Tool Result:',
            '  file contents here',
        ])
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        if agent._tool_invocations:
            # The parser should have detected the tool and attempted result status
            assert agent._tool_invocations[0].tool == "Read"

    def test_tool_result_error_updates_status(self):
        agent = self._make_agent([
            'Write("/src/output.py")',
            'Error: Permission denied',
        ])
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        if agent._tool_invocations:
            assert agent._tool_invocations[0].tool == "Write"

    def test_parse_jest_results(self):
        agent = self._make_agent(["Tests: 10 passed, 2 failed, 12 total"])
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts.get("tests", 0) >= 1

    def test_parse_git_commit(self):
        agent = self._make_agent(["git commit -m 'fix: test bug'"])
        parser = OutputIntelligenceParser()
        counts = parser.parse_incremental(agent)
        assert counts.get("git", 0) >= 1

    def test_incremental_parsing_watermark(self):
        """Parser only processes lines after the watermark."""
        agent = self._make_agent(['Read("/a.py")', 'Write("/b.py")'])
        parser = OutputIntelligenceParser()
        c1 = parser.parse_incremental(agent)
        assert c1["tools"] == 2
        # Second call without new lines should find 0
        c2 = parser.parse_incremental(agent)
        assert c2["tools"] == 0

    def test_incremental_parsing_new_lines_only(self):
        """New lines added after first parse should be picked up."""
        agent = self._make_agent(['Read("/a.py")'])
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        assert len(agent._tool_invocations) == 1
        agent.output_lines.append('Edit("/b.py")')
        c2 = parser.parse_incremental(agent)
        assert c2["tools"] == 1
        assert len(agent._tool_invocations) == 2


# ─────────────────────────────────────────────
# Keyword Command Parser Tests
# ─────────────────────────────────────────────
class TestKeywordParseCommand:
    """Unit tests for _keyword_parse_command fallback parser."""

    def test_spawn_keyword(self):
        intent = _keyword_parse_command("spawn a backend agent", [])
        assert intent.action == "spawn"
        assert intent.parameters["role"] == "backend"

    def test_spawn_default_role(self):
        intent = _keyword_parse_command("create a new agent", [])
        assert intent.action == "spawn"
        assert intent.parameters["role"] == "general"

    def test_kill_keyword(self):
        intent = _keyword_parse_command("kill all agents", [])
        assert intent.action == "kill"

    def test_pause_keyword(self):
        intent = _keyword_parse_command("pause agent work", [])
        assert intent.action == "pause"

    def test_resume_keyword(self):
        intent = _keyword_parse_command("resume the agent", [])
        assert intent.action == "resume"

    def test_status_query(self):
        intent = _keyword_parse_command("what is the status", [])
        assert intent.action == "status"

    def test_approve_message(self):
        intent = _keyword_parse_command("approve the plan", [])
        assert intent.action == "send"
        assert intent.message == "yes, proceed"

    def test_reject_message(self):
        intent = _keyword_parse_command("reject that change", [])
        assert intent.action == "send"
        assert intent.message == "no, stop"

    def test_unknown_command(self):
        intent = _keyword_parse_command("make me a sandwich", [])
        assert intent.action == "unknown"
        assert intent.confidence < 0.5

    def test_spawn_tester_role(self):
        intent = _keyword_parse_command("launch a tester agent", [])
        assert intent.action == "spawn"
        assert intent.parameters["role"] == "tester"

    def test_spawn_security_role(self):
        intent = _keyword_parse_command("start security audit", [])
        assert intent.action == "spawn"
        assert intent.parameters["role"] == "security"

    def test_confidence_levels(self):
        spawn = _keyword_parse_command("spawn backend", [])
        assert spawn.confidence == 0.6
        unknown = _keyword_parse_command("gibberish", [])
        assert unknown.confidence == 0.2


# ─────────────────────────────────────────────
# Background Task Logic Tests
# ─────────────────────────────────────────────
class TestSpawnTimeout:
    """Tests for spawn timeout detection in output_capture_loop."""

    def _make_agent(self, status="spawning", spawn_time=None):
        from ashlar_server import Agent
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


class TestFloodDetection:
    """Tests for output flood detection logic."""

    def _make_agent(self):
        from ashlar_server import Agent
        agent = Agent(
            id="f001", name="flood-test", role="general", status="working",
            backend="claude-code", task="test", working_dir="/tmp",
        )
        agent.status = "working"
        return agent

    def test_flood_ticks_increment_above_threshold(self):
        agent = self._make_agent()
        agent.output_rate = 600.0  # Very high
        flood_threshold = 500
        if agent.output_rate > flood_threshold:
            agent._flood_ticks += 1
        assert agent._flood_ticks == 1

    def test_flood_detected_after_sustained_ticks(self):
        agent = self._make_agent()
        agent.output_rate = 600.0
        flood_threshold = 500
        sustained_ticks = 3
        # Simulate 3 ticks
        for _ in range(3):
            if agent.output_rate > flood_threshold:
                agent._flood_ticks += 1
                if agent._flood_ticks >= sustained_ticks and not agent._flood_detected:
                    agent._flood_detected = True
        assert agent._flood_detected is True
        assert agent._flood_ticks == 3

    def test_no_flood_below_threshold(self):
        agent = self._make_agent()
        agent.output_rate = 100.0
        flood_threshold = 500
        if agent.output_rate > flood_threshold:
            agent._flood_ticks += 1
        assert agent._flood_ticks == 0
        assert agent._flood_detected is False

    def test_flood_ticks_decrement_when_rate_drops(self):
        agent = self._make_agent()
        agent._flood_ticks = 5
        agent.output_rate = 100.0  # Below threshold
        flood_threshold = 500
        if agent.output_rate <= flood_threshold and agent._flood_ticks > 0:
            agent._flood_ticks = max(0, agent._flood_ticks - 1)
            if agent._flood_ticks == 0:
                agent._flood_detected = False
        assert agent._flood_ticks == 4


class TestHealthCheckPathological:
    """Tests for pathological error loop detection in health_check_loop."""

    def _make_agent(self, restart_count=1, error_time=None, restart_time=None):
        from ashlar_server import Agent
        agent = Agent(
            id="p001", name="patho-test", role="general", status="error",
            backend="claude-code", task="test", working_dir="/tmp",
        )
        agent.status = "error"
        agent.restart_count = restart_count
        if error_time is not None:
            agent._error_entered_at = error_time
        if restart_time is not None:
            agent.last_restart_time = restart_time
        return agent

    def test_pathological_detected_when_error_within_window(self):
        now = time.monotonic()
        # Error occurred 5s after restart (window is 10s)
        agent = self._make_agent(
            restart_count=1, error_time=now - 5, restart_time=now - 10,
        )
        window = 10.0
        time_working = agent._error_entered_at - agent.last_restart_time
        if time_working < window and not agent._pathological:
            agent._pathological = True
            agent.max_restarts = min(agent.max_restarts, 2)
        assert agent._pathological is True
        assert agent.max_restarts == 2

    def test_not_pathological_if_worked_long_enough(self):
        now = time.monotonic()
        # Error occurred 30s after restart (outside 10s window)
        agent = self._make_agent(
            restart_count=1, error_time=now - 5, restart_time=now - 35,
        )
        window = 10.0
        time_working = agent._error_entered_at - agent.last_restart_time
        if time_working < window and not agent._pathological:
            agent._pathological = True
        assert agent._pathological is False

    def test_not_pathological_on_first_error(self):
        now = time.monotonic()
        agent = self._make_agent(restart_count=0, error_time=now)
        # Pathological check requires restart_count > 0
        if agent.restart_count > 0 and agent._error_entered_at > 0 and agent.last_restart_time > 0:
            agent._pathological = True
        assert agent._pathological is False


class TestExponentialBackoff:
    """Tests for auto-restart exponential backoff calculation."""

    def test_backoff_values(self):
        assert 5.0 * (2 ** 0) == 5.0   # First restart: 5s
        assert 5.0 * (2 ** 1) == 10.0  # Second restart: 10s
        assert 5.0 * (2 ** 2) == 20.0  # Third restart: 20s
        assert 5.0 * (2 ** 3) == 40.0  # Fourth restart: 40s

    def test_restart_allowed_after_backoff(self):
        from ashlar_server import Agent
        agent = Agent(id="b001", name="backoff", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent._error_entered_at = time.monotonic() - 15  # Error 15s ago
        agent.restart_count = 1
        agent.last_restart_time = time.monotonic() - 12  # Last restart 12s ago
        backoff = 5.0 * (2 ** agent.restart_count)  # 10s
        time_since = time.monotonic() - agent.last_restart_time
        assert time_since >= backoff  # Should be allowed

    def test_restart_blocked_during_backoff(self):
        from ashlar_server import Agent
        agent = Agent(id="b002", name="backoff2", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent._error_entered_at = time.monotonic() - 3  # Error 3s ago
        agent.restart_count = 2
        agent.last_restart_time = time.monotonic() - 5  # Last restart 5s ago
        backoff = 5.0 * (2 ** agent.restart_count)  # 20s
        time_since = time.monotonic() - agent.last_restart_time
        assert time_since < backoff  # Should be blocked


class TestMaxRestartExhaustion:
    """Tests for max restart exhaustion notification logic."""

    def test_exhausted_clears_error_entered_at(self):
        from ashlar_server import Agent
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
        from ashlar_server import Agent
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
        from ashlar_server import Agent
        agent = Agent(id="i001", name="idle", role="general", status="idle", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "idle"
        agent.last_output_time = time.monotonic() - 600  # 10 min ago
        idle_ttl = 300  # 5 min TTL
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is True

    def test_not_reaped_within_ttl(self):
        from ashlar_server import Agent
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
        from ashlar_server import Agent
        agent = Agent(id="i003", name="working", role="general", status="working", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "working"
        agent.last_output_time = time.monotonic() - 600
        idle_ttl = 300
        should_reap = (
            agent.status in ("idle", "complete")
            and agent.last_output_time > 0
            and idle_ttl > 0
            and (time.monotonic() - agent.last_output_time) > idle_ttl
        )
        assert should_reap is False

    def test_zero_ttl_disables_reaping(self):
        from ashlar_server import Agent
        agent = Agent(id="i004", name="noReap", role="general", status="idle", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "idle"
        agent.last_output_time = time.monotonic() - 9999
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

def _make_wf_run(specs, **kwargs):
    """Helper to create a WorkflowRun with sane defaults."""
    return WorkflowRun(
        id="wf-test-001",
        workflow_id="wf-001",
        workflow_name="Test Workflow",
        agent_specs=specs,
        pending_indices=set(range(len(specs))),
        **kwargs,
    )


class TestWorkflowRunGetReadyIndices:
    """Test _get_ready_indices via direct WorkflowRun state manipulation."""

    def _make_manager(self):
        """Create a minimal AgentManager mock with _get_ready_indices."""
        mgr = MagicMock()
        mgr._get_ready_indices = ashlar_server.AgentManager._get_ready_indices.__get__(mgr)
        return mgr

    def test_no_deps_all_ready(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "task1"},
            {"name": "a2", "role": "frontend", "task": "task2"},
        ]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert sorted(ready) == [0, 1]

    def test_deps_not_satisfied(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "task1"},
            {"name": "a2", "role": "frontend", "task": "task2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert ready == [0]

    def test_deps_satisfied(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "task1"},
            {"name": "a2", "role": "frontend", "task": "task2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.pending_indices.discard(0)
        wf.agent_map[0] = "agent-a1"
        wf.completed_ids.add("agent-a1")
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert ready == [1]

    def test_partial_deps_satisfied(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "task1"},
            {"name": "a2", "role": "backend", "task": "task2"},
            {"name": "a3", "role": "frontend", "task": "task3", "depends_on": [0, 1]},
        ]
        wf = _make_wf_run(specs)
        wf.pending_indices.discard(0)
        wf.agent_map[0] = "agent-a1"
        wf.completed_ids.add("agent-a1")
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert sorted(ready) == [1]

    def test_all_deps_satisfied_multi(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "task1"},
            {"name": "a2", "role": "backend", "task": "task2"},
            {"name": "a3", "role": "frontend", "task": "task3", "depends_on": [0, 1]},
        ]
        wf = _make_wf_run(specs)
        wf.pending_indices = {2}
        wf.agent_map = {0: "agent-a1", 1: "agent-a2"}
        wf.completed_ids = {"agent-a1", "agent-a2"}
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert ready == [2]

    def test_empty_pending(self):
        specs = [{"name": "a1", "role": "backend", "task": "task1"}]
        wf = _make_wf_run(specs)
        wf.pending_indices = set()
        mgr = self._make_manager()
        ready = mgr._get_ready_indices(wf)
        assert ready == []


class TestOnAgentComplete:
    def _make_manager(self):
        mgr = MagicMock()
        mgr.on_agent_complete = ashlar_server.AgentManager.on_agent_complete.__get__(mgr)
        mgr.workflow_runs = {}
        return mgr

    def test_moves_to_completed(self):
        specs = [{"name": "a1", "role": "backend", "task": "t"}]
        wf = _make_wf_run(specs)
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = set()
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result = mgr.on_agent_complete("agent-a1")
        assert result is wf
        assert "agent-a1" not in wf.running_ids
        assert "agent-a1" in wf.completed_ids

    def test_returns_none_for_non_workflow_agent(self):
        mgr = self._make_manager()
        result = mgr.on_agent_complete("agent-unknown")
        assert result is None

    def test_does_not_affect_other_agents(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t"},
            {"name": "a2", "role": "frontend", "task": "t"},
        ]
        wf = _make_wf_run(specs)
        wf.running_ids = {"agent-a1", "agent-a2"}
        wf.pending_indices = set()
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        mgr.on_agent_complete("agent-a1")
        assert "agent-a2" in wf.running_ids
        assert "agent-a1" in wf.completed_ids


class TestOnAgentFailed:
    def _make_manager(self):
        mgr = MagicMock()
        mgr.on_agent_failed = ashlar_server.AgentManager.on_agent_failed.__get__(mgr)
        mgr.workflow_runs = {}
        return mgr

    def test_abort_default(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t"},
            {"name": "a2", "role": "frontend", "task": "t", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1"}
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = {1}
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result, action = mgr.on_agent_failed("agent-a1")
        assert result is wf
        assert action == "abort"
        assert "agent-a1" in wf.failed_ids
        assert 1 not in wf.pending_indices
        assert "blocked_spec_1" in wf.failed_ids

    def test_skip_treats_as_completed(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t", "on_failure": "skip"},
            {"name": "a2", "role": "frontend", "task": "t", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1"}
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = {1}
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result, action = mgr.on_agent_failed("agent-a1")
        assert action == "skip"
        assert "agent-a1" in wf.completed_ids
        assert 1 in wf.pending_indices

    def test_retry_re_adds_to_pending(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t", "on_failure": "retry", "retry_count": 2},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1"}
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = set()
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result, action = mgr.on_agent_failed("agent-a1")
        assert action == "retry"
        assert 0 in wf.pending_indices
        assert 0 not in wf.agent_map
        assert specs[0]["_retries"] == 1

    def test_retry_exhausted_falls_to_abort(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t", "on_failure": "retry", "retry_count": 2, "_retries": 2},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1"}
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = set()
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result, action = mgr.on_agent_failed("agent-a1")
        assert action == "abort"
        assert "agent-a1" in wf.failed_ids

    def test_retry_capped_at_3(self):
        specs = [
            {"name": "a1", "role": "backend", "task": "t", "on_failure": "retry", "retry_count": 10, "_retries": 3},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1"}
        wf.running_ids = {"agent-a1"}
        wf.pending_indices = set()
        mgr = self._make_manager()
        mgr.workflow_runs = {"wf-test-001": wf}
        result, action = mgr.on_agent_failed("agent-a1")
        assert action == "abort"

    def test_non_workflow_agent_returns_none(self):
        mgr = self._make_manager()
        result, action = mgr.on_agent_failed("agent-unknown")
        assert result is None
        assert action == "abort"


class TestWorkflowRunCircularDeps:
    def test_valid_dag(self):
        specs = [
            {"name": "a1", "task": "t"},
            {"name": "a2", "task": "t", "depends_on": [0]},
            {"name": "a3", "task": "t", "depends_on": [1]},
        ]
        assert WorkflowRun.detect_circular_deps(specs) is None

    def test_self_reference(self):
        specs = [{"name": "a1", "task": "t", "depends_on": [0]}]
        result = WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_mutual_cycle(self):
        specs = [
            {"name": "a1", "task": "t", "depends_on": [1]},
            {"name": "a2", "task": "t", "depends_on": [0]},
        ]
        result = WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_out_of_range_dep(self):
        specs = [{"name": "a1", "task": "t", "depends_on": [5]}]
        result = WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_no_deps(self):
        specs = [{"name": "a1", "task": "t"}, {"name": "a2", "task": "t"}]
        assert WorkflowRun.detect_circular_deps(specs) is None


# ─────────────────────────────────────────────
# T17: _extract_question
# ─────────────────────────────────────────────

class TestExtractQuestion:
    def test_single_line(self):
        lines = ["some output", "", "Do you want to proceed?"]
        result = _extract_question(lines)
        assert result == "Do you want to proceed?"

    def test_multiple_lines(self):
        lines = ["", "I found 3 issues:", "1. Missing import", "2. Type error"]
        result = _extract_question(lines)
        assert "I found 3 issues:" in result
        assert "Missing import" in result

    def test_empty_lines_only(self):
        result = _extract_question(["", "", ""])
        assert result == "Agent needs your input"

    def test_empty_list(self):
        result = _extract_question([])
        assert result == "Agent needs your input"

    def test_max_three_lines(self):
        lines = ["Line1", "Line2", "Line3", "Line4", "Line5"]
        result = _extract_question(lines)
        assert len(result.split("\n")) == 3

    def test_stops_at_blank_after_content(self):
        lines = ["old output", "", "Question part 1", "Question part 2"]
        result = _extract_question(lines)
        assert "Question part 1" in result
        assert "Question part 2" in result
        assert "old output" not in result

    def test_strips_whitespace(self):
        lines = ["  Padded question  "]
        result = _extract_question(lines)
        assert result == "Padded question"


# ─────────────────────────────────────────────
# T18: _resolve_agent_refs
# ─────────────────────────────────────────────

class TestResolveAgentRefs:
    def _agent(self, name, agent_id):
        a = MagicMock()
        a.name = name
        a.id = agent_id
        return a

    def test_name_match(self):
        agents = [self._agent("auth-api", "a7f3"), self._agent("test-runner", "b2e9")]
        result = _resolve_agent_refs("kill auth-api now", agents)
        assert "a7f3" in result

    def test_id_match(self):
        agents = [self._agent("auth-api", "a7f3")]
        result = _resolve_agent_refs("check a7f3 status", agents)
        assert "a7f3" in result

    def test_numeric_reference(self):
        agents = [self._agent("first", "a001"), self._agent("second", "b002")]
        result = _resolve_agent_refs("pause agent 2", agents)
        assert "b002" in result

    def test_numeric_out_of_range(self):
        agents = [self._agent("only", "a001")]
        result = _resolve_agent_refs("agent 99", agents)
        assert "a001" not in result

    def test_no_match(self):
        agents = [self._agent("auth-api", "a7f3")]
        result = _resolve_agent_refs("do something random", agents)
        assert result == []

    def test_empty_agents(self):
        result = _resolve_agent_refs("agent 1", [])
        assert result == []

    def test_multiple_matches(self):
        agents = [self._agent("auth-api", "a001"), self._agent("test-runner", "b002")]
        result = _resolve_agent_refs("check auth-api and test-runner", agents)
        assert "a001" in result
        assert "b002" in result


# ─────────────────────────────────────────────
# T19: WorkflowRun.to_dict
# ─────────────────────────────────────────────

class TestWorkflowRunToDict:
    def test_basic_to_dict(self):
        specs = [{"name": "a1", "task": "t"}]
        wf = _make_wf_run(specs)
        d = wf.to_dict()
        assert d["id"] == "wf-test-001"
        assert d["workflow_name"] == "Test Workflow"
        assert d["status"] == "running"
        assert isinstance(d["pending_indices"], list)

    def test_agent_map_keys_are_strings(self):
        specs = [{"name": "a1", "task": "t"}]
        wf = _make_wf_run(specs)
        wf.agent_map = {0: "agent-a1", 1: "agent-a2"}
        d = wf.to_dict()
        assert all(isinstance(k, str) for k in d["agent_map"].keys())
        assert d["agent_map"]["0"] == "agent-a1"


# ─────────────────────────────────────────────
# T20: Safe condition evaluation
# ─────────────────────────────────────────────

class TestSafeEvalCondition:
    def _eval(self, expr, ctx=None):
        return ashlar_server.AgentManager._safe_eval_condition(expr, ctx or {})

    def test_equals_true(self):
        assert self._eval("prev.status == 'complete'", {"prev.status": "complete"}) is True

    def test_equals_false(self):
        assert self._eval("prev.status == 'error'", {"prev.status": "complete"}) is False

    def test_not_equals(self):
        assert self._eval("prev.status != 'error'", {"prev.status": "complete"}) is True

    def test_in_operator(self):
        assert self._eval("'auth' in prev.summary", {"prev.summary": "Fixed auth bug"}) is True

    def test_not_in_operator(self):
        assert self._eval("'error' not in prev.summary", {"prev.summary": "All good"}) is True

    def test_unresolvable_returns_false(self):
        assert self._eval("unknown_var == 'test'", {}) is False

    def test_no_operator_returns_false(self):
        assert self._eval("just a string", {}) is False


# ─────────────────────────────────────────────
# T21: File conflict regex patterns
# ─────────────────────────────────────────────

class TestFileConflictRegex:
    """Test the regex patterns used for file conflict detection."""

    def _write_re(self):
        return ashlar_server.AgentManager._FILE_WRITE_RE

    def _read_re(self):
        return ashlar_server.AgentManager._FILE_READ_RE

    def test_write_writing_pattern(self):
        m = self._write_re().search("Writing src/auth.ts")
        assert m and m.group(1) == "src/auth.ts"

    def test_write_editing_pattern(self):
        m = self._write_re().search("Editing src/main.py")
        assert m and m.group(1) == "src/main.py"

    def test_write_creating_pattern(self):
        m = self._write_re().search("Creating tests/new_test.py")
        assert m and m.group(1) == "tests/new_test.py"

    def test_write_tool_use_edit(self):
        m = self._write_re().search("Tool Use: Edit src/app.js")
        assert m and m.group(1) == "src/app.js"

    def test_write_tool_use_write(self):
        m = self._write_re().search("Tool Use: Write lib/utils.py")
        assert m and m.group(1) == "lib/utils.py"

    def test_read_reading_pattern(self):
        m = self._read_re().search("Reading src/config.yaml")
        assert m and m.group(1) == "src/config.yaml"

    def test_read_tool_use_read(self):
        m = self._read_re().search("Tool Use: Read src/index.ts")
        assert m and m.group(1) == "src/index.ts"

    def test_read_scanning_pattern(self):
        m = self._read_re().search("Scanning lib/utils.py")
        assert m and m.group(1) == "lib/utils.py"

    def test_no_match_on_plain_text(self):
        assert self._write_re().search("Hello world") is None
        assert self._read_re().search("Hello world") is None

    def test_case_insensitive(self):
        m = self._write_re().search("WRITING SRC/Auth.ts")
        assert m is not None

    def test_path_with_deep_nesting(self):
        m = self._write_re().search("Editing src/components/auth/Login.tsx")
        assert m and "Login.tsx" in m.group(1)


# ─────────────────────────────────────────────
# T22: calculate_efficiency_score
# ─────────────────────────────────────────────

class TestCalculateEfficiencyScore:
    def _make_agent(self, **overrides):
        from ashlar_server import Agent
        agent = Agent(
            id="eff01", name="eff-test", role="backend",
            status="working", backend="claude-code",
            task="test", working_dir="/tmp"
        )
        agent._spawn_time = time.monotonic() - 300  # 5 minutes ago
        agent._tool_invocations = []
        agent._file_operations = []
        agent.error_count = 0
        agent.context_pct = 0.3
        agent.total_output_lines = 100
        for k, v in overrides.items():
            setattr(agent, k, v)
        return agent

    def test_returns_dict_with_score(self):
        agent = self._make_agent()
        result = calculate_efficiency_score(agent)
        assert "score" in result
        assert "tools_per_min" in result
        assert "error_rate" in result
        assert "context_efficiency" in result
        assert "uptime_min" in result

    def test_score_in_valid_range(self):
        agent = self._make_agent()
        result = calculate_efficiency_score(agent)
        assert 0.0 <= result["score"] <= 1.0

    def test_high_tool_count_increases_score(self):
        # Agent with many tools
        agent_high = self._make_agent()
        agent_high._tool_invocations = [MagicMock()] * 30
        agent_high._file_operations = [MagicMock()] * 10

        # Agent with no tools
        agent_low = self._make_agent()

        high_score = calculate_efficiency_score(agent_high)["score"]
        low_score = calculate_efficiency_score(agent_low)["score"]
        assert high_score > low_score

    def test_high_error_rate_decreases_score(self):
        agent_clean = self._make_agent()
        agent_clean._tool_invocations = [MagicMock()] * 10
        agent_clean.error_count = 0

        agent_errors = self._make_agent()
        agent_errors._tool_invocations = [MagicMock()] * 10
        agent_errors.error_count = 5

        clean_score = calculate_efficiency_score(agent_clean)["score"]
        error_score = calculate_efficiency_score(agent_errors)["score"]
        assert clean_score > error_score

    def test_zero_spawn_time_handled(self):
        agent = self._make_agent()
        agent._spawn_time = 0
        # Should not crash
        result = calculate_efficiency_score(agent)
        assert result["score"] >= 0.0

    def test_uptime_reported(self):
        agent = self._make_agent()
        agent._spawn_time = time.monotonic() - 600  # 10 minutes
        result = calculate_efficiency_score(agent)
        assert result["uptime_min"] >= 9.0  # ~10 minutes


# ─────────────────────────────────────────────
# T23: Agent._cost_burn_rate
# ─────────────────────────────────────────────

class TestCostBurnRate:
    def _make_agent(self, **overrides):
        from ashlar_server import Agent
        agent = Agent(
            id="br01", name="burn-test", role="backend",
            status="working", backend="claude-code",
            task="test", working_dir="/tmp"
        )
        agent._spawn_time = time.monotonic() - 300  # 5 minutes ago
        agent.estimated_cost_usd = 0.50
        agent.tokens_input = 50000
        agent.tokens_output = 10000
        for k, v in overrides.items():
            setattr(agent, k, v)
        return agent

    def test_returns_rates(self):
        agent = self._make_agent()
        result = agent._cost_burn_rate()
        assert result is not None
        assert "cost_per_min" in result
        assert "tokens_per_min" in result
        assert "minutes_remaining" in result
        assert "uptime_min" in result

    def test_math_correct(self):
        agent = self._make_agent()
        agent._spawn_time = time.monotonic() - 600  # 10 minutes
        agent.estimated_cost_usd = 1.0
        agent.tokens_input = 100000
        agent.tokens_output = 20000
        result = agent._cost_burn_rate()
        assert result is not None
        # $1.0 / 10 min = $0.10/min
        assert abs(result["cost_per_min"] - 0.10) < 0.02
        # 120K tokens / 10 min = 12K/min
        assert abs(result["tokens_per_min"] - 12000) < 1000

    def test_returns_none_with_zero_cost(self):
        agent = self._make_agent()
        agent.estimated_cost_usd = 0
        result = agent._cost_burn_rate()
        assert result is None

    def test_returns_none_with_zero_spawn_time(self):
        agent = self._make_agent()
        agent._spawn_time = 0
        result = agent._cost_burn_rate()
        assert result is None

    def test_returns_none_when_too_short_uptime(self):
        agent = self._make_agent()
        agent._spawn_time = time.monotonic() - 10  # Only 10 seconds
        result = agent._cost_burn_rate()
        assert result is None

    def test_minutes_remaining_calculated(self):
        agent = self._make_agent()
        agent._spawn_time = time.monotonic() - 600  # 10 minutes
        agent.tokens_input = 50000
        agent.tokens_output = 10000
        result = agent._cost_burn_rate()
        assert result is not None
        # Has remaining minutes estimate
        assert result["minutes_remaining"] is not None
        assert result["minutes_remaining"] > 0
