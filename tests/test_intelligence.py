"""Tests for IntelligenceClient, NLU command parsing, and agent reference resolution."""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from unittest.mock import patch
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    from ashlr_server import (
        IntelligenceClient,
        ParsedIntent,
        AgentInsight,
        _keyword_parse_command,
        _resolve_agent_refs,
    )


def _make_intel_config(enabled=True, api_key="test-key"):
    """Create a Config with LLM fields set for testing IntelligenceClient."""
    cfg = ashlr_server.Config()
    cfg.llm_enabled = enabled
    cfg.llm_api_key = api_key
    cfg.llm_model = "grok-4.3"
    cfg.llm_base_url = "https://api.x.ai/v1"
    cfg.llm_max_output_lines = 30
    return cfg



class TestIntelligenceClient:
    """Tests for unified IntelligenceClient circuit breaker, error handling, and graceful degradation."""

    def test_check_circuit_disabled_no_key(self):
        """Client with no API key should not be available."""
        cfg = _make_intel_config(enabled=True, api_key="")
        client = IntelligenceClient(cfg)
        assert client.available is False
        assert client._check_circuit() is False

    def test_check_circuit_disabled_flag(self):
        """Client with enabled=False should not be available."""
        cfg = _make_intel_config(enabled=False, api_key="test-key")
        client = IntelligenceClient(cfg)
        assert client.available is False

    def test_check_circuit_enabled(self):
        """Client with key and enabled=True should be available."""
        cfg = _make_intel_config(enabled=True, api_key="test-key")
        client = IntelligenceClient(cfg)
        assert client.available is True
        assert client._check_circuit() is True

    def test_check_circuit_trips_at_max_failures(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        client._failures = 5
        client._circuit_reset_time = time.monotonic() + 60
        assert client._check_circuit() is False

    def test_check_circuit_resets_after_cooldown(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        client._failures = 5
        client._circuit_reset_time = time.monotonic() - 1  # expired
        assert client._check_circuit() is True
        assert client._failures == 0

    def test_available_flag_controls_circuit(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        assert client.available is True
        client.available = False
        assert client._check_circuit() is False

    async def test_call_returns_none_when_circuit_open(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        client.available = False
        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None

    async def test_analyze_fleet_skips_single_agent(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        mock_agent = MagicMock()
        result = await client.analyze_fleet([mock_agent], [])
        assert result == []

    async def test_analyze_fleet_skips_empty_list(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        result = await client.analyze_fleet([], [])
        assert result == []

    async def test_summarize_returns_none_for_empty_output(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        result = await client.summarize([], "task", "general", "working")
        assert result is None

    async def test_parse_command_returns_unknown_when_circuit_open(self):
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        client.available = False
        result = await client.parse_command("test command", [], {})
        assert isinstance(result, ParsedIntent)
        assert result.action == "unknown"
        assert result.confidence == 0.0


# ─────────────────────────────────────────────
# IntelligenceClient HTTP Interaction Tests
# ─────────────────────────────────────────────


def _mock_response(status=200, json_data=None, headers=None):
    """Create a mock aiohttp response as async context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_data or {})

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestIntelligenceClientHTTP:
    """Tests for IntelligenceClient HTTP interaction, response parsing, and error handling."""

    def _make_client_with_session(self, response_status=200, json_data=None, headers=None, side_effect=None):
        """Helper to create an IntelligenceClient with a mocked session."""
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        mock_session = MagicMock()
        mock_session.closed = False
        if side_effect:
            mock_session.post = MagicMock(side_effect=side_effect)
        else:
            mock_session.post = MagicMock(return_value=_mock_response(
                response_status, json_data, headers
            ))
        client._session = mock_session
        return client

    async def test_call_success(self):
        """Successful API call returns content and resets failure count."""
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": "test response"}}]}
        )
        client._failures = 2

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result == "test response"
        assert client._failures == 0

    async def test_call_empty_choices(self):
        """API response with empty choices returns None."""
        client = self._make_client_with_session(200, {"choices": []})

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None

    async def test_call_auth_failure_disables_client(self):
        """HTTP 401/403 disables the client permanently."""
        for status in (401, 403):
            client = self._make_client_with_session(status)

            result = await client._call([{"role": "user", "content": "hi"}])
            assert result is None
            assert client.available is False

    async def test_call_rate_limit_429(self):
        """HTTP 429 increments failures and sets cooldown from Retry-After."""
        client = self._make_client_with_session(429, headers={"Retry-After": "30"})

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None
        assert client._failures == 1
        assert client._circuit_reset_time > time.monotonic()

    async def test_call_server_error_increments_failures(self):
        """HTTP 500 increments failure count."""
        client = self._make_client_with_session(500)

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None
        assert client._failures == 1

    async def test_call_timeout_increments_failures(self):
        """Timeout error increments failure count."""
        client = self._make_client_with_session(side_effect=asyncio.TimeoutError())

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None
        assert client._failures == 1

    async def test_call_network_error_increments_failures(self):
        """Network error increments failure count."""
        client = self._make_client_with_session(side_effect=Exception("connection refused"))

        result = await client._call([{"role": "user", "content": "hi"}])
        assert result is None
        assert client._failures == 1

    async def test_circuit_breaker_trips_after_5_failures(self):
        """After 5 consecutive failures, circuit breaker trips with 60s cooldown."""
        client = self._make_client_with_session(500)

        for _ in range(5):
            await client._call([{"role": "user", "content": "hi"}])

        assert client._failures == 5
        assert client._circuit_reset_time > time.monotonic()
        assert client._check_circuit() is False

    async def test_summarize_success(self):
        """summarize() returns truncated summary from LLM response."""
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": "Refactoring auth module to use JWT tokens"}}]}
        )

        result = await client.summarize(
            ["line1", "line2"], "add auth", "backend", "working"
        )
        assert result == "Refactoring auth module to use JWT tokens"

    async def test_summarize_truncates_long_response(self):
        """summarize() truncates responses longer than 100 chars."""
        long_text = "x" * 200
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": long_text}}]}
        )

        result = await client.summarize(["line"], "task", "general", "working")
        assert len(result) == 100

    async def test_parse_command_success(self):
        """parse_command() parses valid JSON into ParsedIntent."""
        response_json = json.dumps({
            "action": "spawn",
            "targets": [],
            "filter": "",
            "message": "",
            "parameters": {"role": "backend", "task": "build auth"},
            "confidence": 0.9,
        })
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": response_json}}]}
        )

        result = await client.parse_command("spawn a backend for auth", [], {})
        assert isinstance(result, ParsedIntent)
        assert result.action == "spawn"
        assert result.confidence == 0.9
        assert result.parameters["role"] == "backend"

    async def test_parse_command_invalid_json_fallback(self):
        """parse_command() returns unknown intent on invalid JSON response."""
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": "not valid json {{"}}]}
        )

        result = await client.parse_command("test", [], {})
        assert result.action == "unknown"
        assert result.confidence == 0.0

    async def test_analyze_fleet_success(self):
        """analyze_fleet() parses valid insights from LLM response."""
        insights_json = json.dumps([
            {
                "type": "conflict",
                "severity": "warning",
                "message": "Agents editing same file",
                "agent_ids": ["a1", "a2"],
                "suggested_action": "Coordinate file access",
            }
        ])
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": insights_json}}]}
        )

        def make_fleet_agent(name, agent_id, role="backend", status="working"):
            a = MagicMock()
            a.name = name; a.id = agent_id; a.role = role; a.status = status
            a.summary = "working on things"; a.task = "test task"
            a.health_score = 0.9; a.context_pct = 0.3
            a._file_operations = []; a._tool_invocations = []
            return a

        agents = [make_fleet_agent("auth-api", "a1"), make_fleet_agent("test-runner", "a2", role="tester")]
        result = await client.analyze_fleet(agents, [])
        assert len(result) == 1
        assert result[0].insight_type == "conflict"
        assert result[0].severity == "warning"
        assert "a1" in result[0].agent_ids

    def _make_fleet_agents(self):
        """Create two mock agents for fleet analysis tests."""
        agents = []
        for name, aid, role in [("a1", "a1", "backend"), ("a2", "a2", "tester")]:
            a = MagicMock()
            a.name = name; a.id = aid; a.role = role; a.status = "working"
            a.summary = "test"; a.task = "test"; a.health_score = 1.0; a.context_pct = 0.1
            a._file_operations = []; a._tool_invocations = []
            agents.append(a)
        return agents

    async def test_analyze_fleet_invalid_json_returns_empty(self):
        """analyze_fleet() returns empty list on invalid JSON."""
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": "not json"}}]}
        )
        result = await client.analyze_fleet(self._make_fleet_agents(), [])
        assert result == []

    async def test_analyze_fleet_caps_at_10_insights(self):
        """analyze_fleet() caps insights at 10."""
        insights = [{"type": "suggestion", "severity": "info", "message": f"insight {i}"} for i in range(15)]
        client = self._make_client_with_session(
            200, {"choices": [{"message": {"content": json.dumps(insights)}}]}
        )
        result = await client.analyze_fleet(self._make_fleet_agents(), [])
        assert len(result) == 10

    async def test_close_closes_session(self):
        """close() closes the aiohttp session."""
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session
        await client.close()
        mock_session.close.assert_awaited_once()

    async def test_close_noop_when_no_session(self):
        """close() is a no-op when no session exists."""
        cfg = _make_intel_config()
        client = IntelligenceClient(cfg)
        await client.close()


# ─────────────────────────────────────────────
# T20: OutputIntelligenceParser Extended Tests
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



