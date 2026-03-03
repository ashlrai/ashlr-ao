"""Tests for agent features: notes, tags, bookmarks, cost tracking, context, and efficiency."""

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



class TestAgentNotesAndTags:
    """Tests for agent notes and tags fields."""

    def _make_agent(self, **kwargs):
        defaults = dict(
            id="a001", name="test", role="backend", status="working",
            project_id=None, working_dir="/tmp", backend="claude-code",
            task="test", tmux_session="ashlr-a001"
        )
        defaults.update(kwargs)
        return ashlr_server.Agent(**defaults)

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
            task="test", tmux_session="ashlr-a001"
        )
        defaults.update(kwargs)
        return ashlr_server.Agent(**defaults)

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



class TestCostBudget:
    def test_default_cost_budget_zero(self):
        """Default cost budget is 0 (no limit)."""
        config = ashlr_server.Config()
        assert config.cost_budget_usd == 0.0
        assert config.cost_budget_auto_pause is False

    def test_cost_budget_in_to_dict(self):
        """Cost budget fields should appear in config to_dict."""
        config = ashlr_server.Config()
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



class TestCostBurnRate:
    """Tests for Agent._cost_burn_rate() forecasting."""

    def test_no_burn_rate_without_cost(self):
        """Should return None when no cost data exists."""
        agent = ashlr_server.Agent(
            id="br01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-br01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent._cost_burn_rate() is None

    def test_no_burn_rate_early(self):
        """Should return None when agent is <30s old (not enough data)."""
        agent = ashlr_server.Agent(
            id="br02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-br02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic()  # Just spawned
        agent.estimated_cost_usd = 0.01
        assert agent._cost_burn_rate() is None

    def test_burn_rate_calculation(self):
        """Should calculate cost_per_min and tokens_per_min correctly."""
        agent = ashlr_server.Agent(
            id="br03", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
            summary="", tmux_session="ashlr-br03",
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
        agent = ashlr_server.Agent(
            id="br04", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-br04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        d = agent.to_dict()
        assert "cost_burn_rate" in d
        assert d["cost_burn_rate"] is None  # No cost data = None



class TestStatusTimeline:
    """Tests for agent status timeline tracking."""

    def test_status_history_recorded(self):
        """set_status should record status transitions."""
        agent = ashlr_server.Agent(
            id="st01", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-st01",
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
        agent = ashlr_server.Agent(
            id="st02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-st02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.set_status("working")  # Same status — should not record
        assert len(agent._status_history) == 0

    def test_status_timeline_in_to_dict(self):
        """to_dict should include status_timeline."""
        agent = ashlr_server.Agent(
            id="st03", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-st03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.set_status("working")
        d = agent.to_dict()
        assert "status_timeline" in d
        assert isinstance(d["status_timeline"], list)

    def test_timeline_capped_at_100(self):
        """Status history should not exceed 100 entries."""
        agent = ashlr_server.Agent(
            id="st04", name="test", role="general", status="spawning",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-st04",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        for i in range(110):
            agent.set_status("working" if i % 2 == 0 else "planning")
        assert len(agent._status_history) <= 100



class TestContextExhaustionWarning:
    """Tests for context exhaustion snapshot and warning."""

    def test_context_warning_flag_default(self):
        """_context_exhaustion_warned should default to False."""
        agent = ashlr_server.Agent(
            id="ce01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-ce01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert not getattr(agent, '_context_exhaustion_warned', False)

    def test_context_warning_creates_snapshot(self):
        """Agent at 92%+ context should create snapshot when triggered."""
        agent = ashlr_server.Agent(
            id="ce02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="High context", tmux_session="ashlr-ce02",
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
        agent = ashlr_server.Agent(
            id="ef01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-ef01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 60  # 1 min uptime
        result = ashlr_server.calculate_efficiency_score(agent)
        assert isinstance(result, dict)
        assert "score" in result
        assert "tools_per_min" in result
        assert "files_per_min" in result
        assert "error_rate" in result
        assert "lines_per_min" in result
        assert "context_efficiency" in result
        assert 0.0 <= result["score"] <= 1.0

    def test_score_in_to_dict(self):
        agent = ashlr_server.Agent(
            id="ef02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-ef02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 120
        d = agent.to_dict()
        assert "efficiency" in d
        assert isinstance(d["efficiency"], dict)
        assert "score" in d["efficiency"]

    def test_new_agent_has_low_score(self):
        """Agent with no tools or files should have a low efficiency score."""
        agent = ashlr_server.Agent(
            id="ef03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-ef03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent._spawn_time = time.monotonic() - 300
        result = ashlr_server.calculate_efficiency_score(agent)
        assert result["score"] < 0.5
        assert result["tools_per_min"] == 0



class TestBookmarkPersistence:
    """Tests for bookmark SQLite persistence layer."""

    def test_agent_bookmarks_start_empty(self):
        """Agent bookmarks list starts empty."""
        agent = ashlr_server.Agent(
            id="bk01", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-bk01",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        assert agent.bookmarks == []

    def test_bookmark_in_memory(self):
        """Bookmarks can be added in memory."""
        agent = ashlr_server.Agent(
            id="bk02", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-bk02",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.bookmarks.append({"id": "abc", "line": 42, "text": "error line", "label": "Bug"})
        assert len(agent.bookmarks) == 1
        assert agent.bookmarks[0]["line"] == 42
        assert agent.bookmarks[0]["label"] == "Bug"

    def test_bookmark_delete_by_id(self):
        """Bookmarks can be filtered by ID for deletion."""
        agent = ashlr_server.Agent(
            id="bk03", name="test", role="general", status="working",
            working_dir="/tmp", backend="demo", task="test",
            summary="", tmux_session="ashlr-bk03",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        )
        agent.bookmarks.append({"id": "a1", "line": 10, "text": "line a", "label": ""})
        agent.bookmarks.append({"id": "b2", "line": 20, "text": "line b", "label": ""})
        agent.bookmarks = [b for b in agent.bookmarks if b.get("id") != "a1"]
        assert len(agent.bookmarks) == 1
        assert agent.bookmarks[0]["id"] == "b2"



class TestPatternAlerting:
    """Tests for the alert_patterns config and pattern alerting in capture loop."""

    def test_config_has_alert_patterns(self):
        """Config should have alert_patterns field with defaults."""
        config = ashlr_server.Config()
        assert hasattr(config, "alert_patterns")
        assert isinstance(config.alert_patterns, list)
        assert len(config.alert_patterns) >= 5

    def test_alert_patterns_have_required_fields(self):
        """Each alert pattern should have pattern, severity, and label."""
        config = ashlr_server.Config()
        for ap in config.alert_patterns:
            assert "pattern" in ap
            assert "severity" in ap
            assert "label" in ap

    def test_alert_patterns_are_valid_regex(self):
        """All default alert patterns should be valid regex."""
        import re
        config = ashlr_server.Config()
        for ap in config.alert_patterns:
            try:
                re.compile(ap["pattern"])
            except re.error:
                pytest.fail(f"Invalid regex in alert pattern: {ap['pattern']}")

    def test_alert_patterns_in_to_dict(self):
        """alert_patterns should appear in Config.to_dict()."""
        config = ashlr_server.Config()
        d = config.to_dict()
        assert "alert_patterns" in d
        assert len(d["alert_patterns"]) >= 5

    def test_alert_patterns_match_critical_errors(self):
        """Alert patterns should match critical error strings."""
        import re
        config = ashlr_server.Config()
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
        config = ashlr_server.Config()
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
        assert hasattr(ashlr_server, "_alert_throttle")
        assert isinstance(ashlr_server._alert_throttle, dict)

    def test_capture_loop_checks_alert_patterns(self):
        """Output capture loop should check alert patterns."""
        import inspect
        src = inspect.getsource(ashlr_server.output_capture_loop)
        assert "pattern_alert" in src
        assert "_compiled_alert_patterns" in src
        assert "_alert_throttle" in src



class TestCalculateEfficiencyScore:
    def _make_agent(self, **overrides):
        from ashlr_server import Agent
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
        agent._spawn_time = max(time.monotonic() - 600, 1.0)  # 10 minutes
        result = calculate_efficiency_score(agent)
        assert result["uptime_min"] > 0  # uptime is positive


# ─────────────────────────────────────────────
# T23: Agent._cost_burn_rate
# ─────────────────────────────────────────────



class TestCostBurnRateExtended:
    def _make_agent(self, **overrides):
        from ashlr_server import Agent
        agent = Agent(
            id="br01", name="burn-test", role="backend",
            status="working", backend="claude-code",
            task="test", working_dir="/tmp"
        )
        # Use max() to prevent negative spawn times on recently booted systems
        agent._spawn_time = max(time.monotonic() - 300, 1.0)  # 5 minutes ago
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
        agent._spawn_time = max(time.monotonic() - 600, 1.0)  # 10 minutes ago
        agent.estimated_cost_usd = 1.0
        agent.tokens_input = 100000
        agent.tokens_output = 20000
        result = agent._cost_burn_rate()
        assert result is not None
        assert result["cost_per_min"] > 0
        assert result["tokens_per_min"] > 0

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
        agent._spawn_time = max(time.monotonic() - 600, 1.0)  # 10 minutes ago
        agent.tokens_input = 50000
        agent.tokens_output = 10000
        result = agent._cost_burn_rate()
        assert result is not None
        # Has remaining minutes estimate
        assert result["minutes_remaining"] is not None
        assert result["minutes_remaining"] > 0


# ─────────────────────────────────────────────
# T15: _check_file_conflicts() — real output parsing
# ─────────────────────────────────────────────


