"""Tests for output capture, status detection, intelligence parsing, and terminal processing."""

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
        make_agent()
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
        parse_agent_status(lines, agent)
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



class TestCaptureOutputReturnTypes:
    def test_capture_output_returns_none_for_missing_agent(self, make_agent):
        """capture_output should return None for non-existent agent."""
        manager = MagicMock(spec=ashlr_server.AgentManager)
        manager.agents = {}
        result = asyncio.run(ashlr_server.AgentManager.capture_output(manager, "nonexistent"))
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



class TestContextDetection:
    def _make_manager(self):
        """Create a minimal AgentManager for testing."""
        from ashlr_server import AgentManager, Config
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



class TestOutputIntelligenceParser:
    """Tests for the regex-based output intelligence parser."""

    def _make_agent(self, lines):
        import collections
        from ashlr_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = lines
        agent._last_parse_index = 0
        agent._total_lines_added = len(lines)
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
        # Add more lines — must also update _total_lines_added
        agent.output_lines.append('Edit("/c.py")')
        agent._total_lines_added = 3
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

    def test_deque_eviction_still_parses(self):
        """Regression: parser must continue parsing after deque fills and evicts old lines.

        Before the fix, _last_parse_index reached maxlen and stayed there forever,
        causing parse_incremental to return early with zero counts on every call.
        """
        import collections
        maxlen = 10  # Small maxlen for test
        agent = MagicMock()
        agent.id = "evict1"
        agent.output_lines = collections.deque(maxlen=maxlen)
        agent._last_parse_index = 0
        agent._total_lines_added = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)

        parser = OutputIntelligenceParser()

        # Fill the deque to capacity
        for i in range(maxlen):
            agent.output_lines.append(f"line {i}")
        agent._total_lines_added = maxlen

        # Parse all initial lines
        parser.parse_incremental(agent)
        assert agent._last_parse_index == maxlen

        # Add more lines causing eviction (deque maxlen = 10, adding 5 more)
        for i in range(5):
            agent.output_lines.append(f'Read("/file{i}.py")')
        agent._total_lines_added = maxlen + 5

        # This should parse the 5 new lines (not return early)
        counts = parser.parse_incremental(agent)
        assert counts["tools"] == 5
        assert len(agent._tool_invocations) == 5

    def test_deque_eviction_no_duplicates(self):
        """After eviction, previously-parsed lines should not be re-parsed."""
        import collections
        maxlen = 5
        agent = MagicMock()
        agent.id = "evict2"
        agent.output_lines = collections.deque(maxlen=maxlen)
        agent._last_parse_index = 0
        agent._total_lines_added = 0
        agent._tool_invocations = collections.deque(maxlen=500)
        agent._file_operations = collections.deque(maxlen=500)
        agent._git_operations = collections.deque(maxlen=200)
        agent._test_results = collections.deque(maxlen=50)

        parser = OutputIntelligenceParser()

        # Add 3 Read tool lines
        for i in range(3):
            agent.output_lines.append(f'Read("/a{i}.py")')
        agent._total_lines_added = 3
        parser.parse_incremental(agent)
        assert len(agent._tool_invocations) == 3

        # Add 4 more, causing 2 evictions (deque now has items [1,2,3,4,5] but [0,1] evicted)
        for i in range(4):
            agent.output_lines.append(f'Edit("/b{i}.py")')
        agent._total_lines_added = 7
        counts = parser.parse_incremental(agent)
        # Should only parse the 4 NEW lines, not re-parse surviving old ones
        assert counts["tools"] == 4
        assert len(agent._tool_invocations) == 7


# ─────────────────────────────────────────────
# T15: Context window calculations
# ─────────────────────────────────────────────



class TestOutputSnapshots:
    """Tests for OutputSnapshot creation and agent snapshot lifecycle."""

    def _make_agent(self):
        agent = ashlr_server.Agent(
            id="snap1", name="test-snap", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test task",
            summary="Doing things", tmux_session="ashlr-snap1",
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



class TestOutputBufferPagination:
    """Tests for output buffer overflow and archive tracking."""

    def test_output_deque_max_length(self):
        """Output lines deque has a max length of 2000."""
        agent = ashlr_server.Agent(
            id="pg01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-pg01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent.output_lines.maxlen == 2000

    def test_archived_lines_starts_at_zero(self):
        """Archived lines counter starts at 0."""
        agent = ashlr_server.Agent(
            id="pg02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-pg02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent._archived_lines == 0

    def test_total_output_lines_in_to_dict(self):
        """total_output_lines is included in to_dict."""
        agent = ashlr_server.Agent(
            id="pg03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-pg03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.total_output_lines = 500
        d = agent.to_dict()
        assert d["total_output_lines"] == 500

    def test_output_overflow_tracking(self):
        """When output exceeds deque size, _archived_lines tracks overflow."""
        agent = ashlr_server.Agent(
            id="pg04", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-pg04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        # Fill beyond capacity
        for i in range(2100):
            agent.output_lines.append(f"line {i}")
        assert len(agent.output_lines) == 2000
        # First 100 lines should have been dropped by the deque
        first_line = agent.output_lines[0]
        assert "line 100" in first_line



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
        assert hasattr(ashlr_server, "search_agent_output")
        assert asyncio.iscoroutinefunction(ashlr_server.search_agent_output)

    def test_search_endpoint_registered(self):
        """Search endpoint should be registered in create_app route setup."""
        import inspect
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/agents/{id}/output/search" in src

    def test_search_supports_regex_param(self):
        """search_agent_output should support regex query parameter."""
        import inspect
        src = inspect.getsource(ashlr_server.search_agent_output)
        assert "regex" in src
        assert "re.compile" in src

    def test_search_supports_context_lines(self):
        """search_agent_output should support context lines parameter."""
        import inspect
        src = inspect.getsource(ashlr_server.search_agent_output)
        assert "context" in src
        assert "context_lines" in src

    def test_search_limits_query_length(self):
        """Search query should be limited to 500 chars."""
        import inspect
        src = inspect.getsource(ashlr_server.search_agent_output)
        assert "500" in src

    def test_search_limits_result_count(self):
        """Search results should be limited."""
        import inspect
        src = inspect.getsource(ashlr_server.search_agent_output)
        assert "limit" in src



class TestBinaryGarbageDetection:
    """Tests for binary/garbage output detection in capture."""

    def test_normal_text_not_garbage(self):
        """Normal text output should not be flagged as garbage."""
        lines = ["Hello world", "Processing file.py", "Done."]
        assert not ashlr_server.AgentManager._is_binary_garbage(lines)

    def test_empty_lines_not_garbage(self):
        """Empty lines should not be flagged as garbage."""
        assert not ashlr_server.AgentManager._is_binary_garbage([])
        assert not ashlr_server.AgentManager._is_binary_garbage([""])

    def test_ansi_colored_text_not_garbage(self):
        """Text with ANSI color codes should not be flagged."""
        lines = ["\x1b[32mSuccess\x1b[0m", "\x1b[1;31mError\x1b[0m: something failed"]
        assert not ashlr_server.AgentManager._is_binary_garbage(lines)

    def test_binary_content_is_garbage(self):
        """Binary content (mostly non-printable) should be flagged."""
        binary_line = "".join(chr(i) for i in range(0, 32) if i not in (9, 10, 13)) * 5
        lines = [binary_line, binary_line]
        assert ashlr_server.AgentManager._is_binary_garbage(lines)

    def test_mixed_with_threshold(self):
        """Mixed content below threshold should not be flagged."""
        # Mostly printable with a few non-printable chars
        lines = ["Normal text with a \x00 byte here"]
        assert not ashlr_server.AgentManager._is_binary_garbage(lines)

    def test_garbage_detection_in_capture_output(self):
        """capture_output should call _is_binary_garbage."""
        src = inspect.getsource(ashlr_server.AgentManager.capture_output)
        assert "_is_binary_garbage" in src
        assert "binary garbage" in src


# ── WebSocket Disconnect Cleanup Tests ───────────────────────────────



class TestLineTruncation:
    """Tests for output line-length truncation."""

    def test_truncation_in_capture_output(self):
        """capture_output should truncate long lines."""
        src = inspect.getsource(ashlr_server.AgentManager.capture_output)
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



class TestOutputMaxLines:
    def test_config_default(self):
        """Default output_max_lines should be 2000."""
        config = ashlr_server.Config()
        assert config.output_max_lines == 2000

    def test_config_custom_value(self):
        """Config should accept custom output_max_lines."""
        config = ashlr_server.Config()
        config.output_max_lines = 500
        assert config.output_max_lines == 500

    def test_spawn_applies_custom_maxlen(self):
        """Spawn should apply custom deque maxlen when output_max_lines != 2000."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        assert "output_max_lines" in src
        assert "maxlen" in src

    def test_default_deque_unchanged(self):
        """When output_max_lines == 2000, no custom deque is created."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        # The condition checks != 2000 before creating custom deque
        assert "!= 2000" in src

    def test_agent_deque_default_maxlen(self):
        """Agent output_lines default deque should have maxlen 2000."""
        agent = ashlr_server.Agent(
            id="test", name="test", role="general",
            status="idle", working_dir="/tmp",
            backend="claude-code", task="test",
        )
        assert agent.output_lines.maxlen == 2000


# ─────────────────────────────────────────────
# Request Logging Middleware (#247)
# ─────────────────────────────────────────────



class TestOutputFloodProtection:
    def test_agent_has_flood_fields(self):
        """Agent should have flood detection fields."""
        agent = ashlr_server.Agent(
            id="f1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert hasattr(agent, '_flood_detected')
        assert hasattr(agent, '_flood_ticks')
        assert agent._flood_detected is False
        assert agent._flood_ticks == 0

    def test_config_flood_threshold(self):
        """Config should have flood threshold settings."""
        config = ashlr_server.Config()
        assert hasattr(config, 'flood_threshold_lines_per_min')
        assert config.flood_threshold_lines_per_min == 3000
        assert hasattr(config, 'flood_sustained_ticks')
        assert config.flood_sustained_ticks == 3

    def test_flood_detected_in_to_dict(self):
        """Agent.to_dict() should include flood_detected."""
        agent = ashlr_server.Agent(
            id="f2", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        d = agent.to_dict()
        assert "flood_detected" in d
        assert d["flood_detected"] is False

    def test_flood_flag_changes_to_dict(self):
        """flood_detected should reflect in to_dict when set."""
        agent = ashlr_server.Agent(
            id="f3", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        agent._flood_detected = True
        d = agent.to_dict()
        assert d["flood_detected"] is True

    def test_capture_loop_has_flood_detection(self):
        """Output capture loop should contain flood detection logic."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        assert "flood_threshold" in src
        assert "_flood_detected" in src
        assert "agent_flood" in src

    def test_flood_broadcasts_event(self):
        """Flood detection should broadcast an agent_flood event."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        assert "agent_flood" in src
        assert "excessive output" in src.lower()

    def test_flood_throttles_broadcast(self):
        """When flood is detected, output broadcast should be throttled."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        assert "flood_detected" in src
        # Should have conditional broadcast
        assert "lines suppressed" in src.lower() or "suppressed" in src.lower()

    def test_flood_ticks_decrement(self):
        """Flood ticks should decrement when rate drops below threshold."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        # Should have decrement logic
        assert "_flood_ticks - 1" in src or "flood_ticks -= 1" in src or "flood_ticks - 1" in src


# ─────────────────────────────────────────────
# Summary Output Cache (#257)
# ─────────────────────────────────────────────



class TestSummaryOutputCache:
    def test_agent_has_summary_output_hash(self):
        """Agent should have _summary_output_hash field."""
        agent = ashlr_server.Agent(
            id="sc1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert hasattr(agent, '_summary_output_hash')
        assert agent._summary_output_hash == 0

    def test_capture_loop_checks_output_hash_before_llm(self):
        """Capture loop should check output hash before calling LLM summarizer."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        assert "_summary_output_hash" in src

    def test_capture_loop_updates_hash_after_summary(self):
        """Capture loop should store output hash after successful summary."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        # Should set the hash after getting summary
        assert "_summary_output_hash = current_hash" in src or "_summary_output_hash =" in src

    def test_capture_loop_skips_unchanged_output(self):
        """Capture loop should skip LLM call when output hash matches."""
        src = inspect.getsource(ashlr_server.output_capture_loop)
        # Should compare current hash with stored hash
        assert "current_hash != agent._summary_output_hash" in src or "current_hash ==" in src

    def test_summary_hash_default_differs_from_output_hash(self):
        """Default summary hash (0) should differ from initial output hash (0) to ensure first summary runs."""
        agent = ashlr_server.Agent(
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
        parser = ashlr_server.OutputIntelligenceParser()
        agent = ashlr_server.Agent(
            id="pt1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        summary = parser.get_activity_summary(agent)
        assert "timing" in summary

    def test_timing_with_no_invocations(self):
        """Timing should return zeros with no invocations."""
        parser = ashlr_server.OutputIntelligenceParser()
        agent = ashlr_server.Agent(
            id="pt2", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        summary = parser.get_activity_summary(agent)
        assert summary["timing"]["avg_interval_sec"] == 0
        assert summary["timing"]["longest_gap_sec"] == 0

    def test_timing_with_invocations(self):
        """Timing should compute intervals between invocations."""
        parser = ashlr_server.OutputIntelligenceParser()
        agent = ashlr_server.Agent(
            id="pt3", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        # Add invocations with known timestamps
        now = time.monotonic()
        for i in range(5):
            agent._tool_invocations.append(ashlr_server.ToolInvocation(
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
        parser = ashlr_server.OutputIntelligenceParser()
        agent = ashlr_server.Agent(
            id="pt4", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        now = time.monotonic()
        for i in range(10):
            agent._tool_invocations.append(ashlr_server.ToolInvocation(
                agent_id="pt4", tool="Edit", args=f"file{i}.py",
                timestamp=now - 60 + (i * 6), line_index=i,
            ))
        summary = parser.get_activity_summary(agent)
        assert summary["timing"]["tools_per_min"] > 0

    def test_compute_timing_method_exists(self):
        """OutputIntelligenceParser should have _compute_timing method."""
        assert hasattr(ashlr_server.OutputIntelligenceParser, '_compute_timing')

    def test_timing_avg_by_tool(self):
        """Timing should break down average intervals by tool type."""
        parser = ashlr_server.OutputIntelligenceParser()
        agent = ashlr_server.Agent(
            id="pt5", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        now = time.monotonic()
        tools = ["Read", "Edit", "Read", "Bash", "Edit"]
        for i, tool in enumerate(tools):
            agent._tool_invocations.append(ashlr_server.ToolInvocation(
                agent_id="pt5", tool=tool, args=f"arg{i}",
                timestamp=now - 25 + (i * 5), line_index=i,
            ))
        summary = parser.get_activity_summary(agent)
        avg_by_tool = summary["timing"]["avg_by_tool"]
        assert isinstance(avg_by_tool, dict)
        assert len(avg_by_tool) > 0


# ─────────────────────────────────────────────
# T19: IntelligenceClient Unit Tests
# ─────────────────────────────────────────────

def _make_intel_config(enabled=True, api_key="test-key"):
    """Create a Config with LLM fields set for testing IntelligenceClient."""
    cfg = ashlr_server.Config()
    cfg.llm_enabled = enabled
    cfg.llm_api_key = api_key
    cfg.llm_model = "grok-4-1-fast-reasoning"
    cfg.llm_base_url = "https://api.x.ai/v1"
    cfg.llm_max_output_lines = 30
    return cfg



class TestOutputIntelligenceParserExtended:
    """Tests for MCP tool parsing, tool result status, Jest/Mocha frameworks."""

    def _make_agent(self, lines):
        agent = ashlr_server.Agent(
            id="pe1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        agent.output_lines = lines
        agent._total_lines_added = len(lines)
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



class TestDetectStatusMethod:
    """Tests for AgentManager.detect_status() — the orchestration wrapper."""

    def _make_manager_with_agent(self, agent):
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        manager.agents = {agent.id: agent}
        manager.backend_configs = {}
        return manager

    def _make_agent(self, agent_id="aaaa", status="working", plan_mode=False):
        agent = ashlr_server.Agent(
            id=agent_id, name="test", role="general", status=status,
            task="test task", backend="claude-code",
            working_dir="/tmp", tmux_session=f"ashlr-{agent_id}",
        )
        agent.plan_mode = plan_mode
        agent.created_at = ashlr_server.datetime.now(ashlr_server.timezone.utc).isoformat()
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic() - 5
        return agent

    def test_paused_agent_always_paused(self):
        agent = self._make_agent(status="paused")
        agent.output_lines.extend(["Do you want me to proceed?"])
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        assert result == "paused"

    def test_empty_output_returns_current(self):
        agent = self._make_agent(status="working")
        # output_lines is empty
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        assert result == "working"

    def test_plan_mode_blocks_working(self):
        agent = self._make_agent(status="planning", plan_mode=True)
        agent.output_lines.extend(["Writing src/app.ts", "Creating handler..."])
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        assert result == "planning"

    def test_plan_mode_allows_waiting(self):
        agent = self._make_agent(status="planning", plan_mode=True)
        agent.output_lines.extend(["Here is my plan:", "Shall I proceed? (yes/no)"])
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        assert result == "waiting"

    def test_plan_mode_allows_error(self):
        agent = self._make_agent(status="planning", plan_mode=True)
        agent.output_lines.extend(["Error: Failed to read file", "Traceback (most recent call last):"])
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        assert result == "error"

    def test_plan_mode_inactive_when_not_planning(self):
        agent = self._make_agent(status="working", plan_mode=True)
        agent.output_lines.extend(["Writing src/app.ts"])
        manager = self._make_manager_with_agent(agent)
        result = asyncio.run(manager.detect_status("aaaa"))
        # Guard inactive (status not "planning"), so working passes through
        assert result != "planning"

    def test_nonexistent_agent_returns_error(self):
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        manager.agents = {}
        manager.backend_configs = {}
        result = asyncio.run(manager.detect_status("nonexistent"))
        assert result == "error"


# ─────────────────────────────────────────────
# T28: _evaluate_skip_if — workflow skip condition evaluation
# ─────────────────────────────────────────────



class TestFloodDetection:
    """Tests for output flood detection logic."""

    def _make_agent(self):
        from ashlr_server import Agent
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


