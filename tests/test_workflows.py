"""Tests for workflow DAG execution, deadlock detection, cascade protection, and dependency resolution."""

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
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is None

    def test_no_deps_returns_none(self):
        """Specs with no depends_on at all should return None."""
        specs = [{"role": "backend"}, {"role": "frontend"}, {"role": "tester"}]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is None

    def test_simple_cycle_detected(self):
        """A→B→A cycle should be detected."""
        specs = [
            {"role": "backend", "depends_on": [1]},
            {"role": "tester", "depends_on": [0]},
        ]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None
        assert len(result) >= 1

    def test_three_node_cycle_detected(self):
        """A→B→C→A cycle should be detected."""
        specs = [
            {"role": "a", "depends_on": [2]},
            {"role": "b", "depends_on": [0]},
            {"role": "c", "depends_on": [1]},
        ]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_self_loop_detected(self):
        """A node depending on itself should be detected."""
        specs = [{"role": "backend", "depends_on": [0]}]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_out_of_range_dep_detected(self):
        """Dependencies referencing out-of-range indices should be flagged."""
        specs = [
            {"role": "backend", "depends_on": [5]},
        ]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None
        assert result[0] == [0, 5]

    def test_negative_dep_detected(self):
        """Negative dependency indices should be flagged."""
        specs = [
            {"role": "backend", "depends_on": [-1]},
        ]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    def test_empty_specs_returns_none(self):
        """Empty spec list should return None (no cycles)."""
        result = ashlr_server.WorkflowRun.detect_circular_deps([])
        assert result is None

    def test_mixed_valid_and_cycle(self):
        """Mix of valid deps and a cycle should detect the cycle."""
        specs = [
            {"role": "a", "depends_on": []},
            {"role": "b", "depends_on": [0]},
            {"role": "c", "depends_on": [3]},
            {"role": "d", "depends_on": [2]},
        ]
        result = ashlr_server.WorkflowRun.detect_circular_deps(specs)
        assert result is not None

    # ── WorkflowRun fields ──

    def test_stage_timeout_sec_default(self):
        """Default stage_timeout_sec should be 1800.0 (30 min)."""
        wf = ashlr_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[], agent_map={}, pending_indices=set(),
        )
        assert wf.stage_timeout_sec == 1800.0

    def test_stage_started_at_default_empty(self):
        """Default stage_started_at should be empty dict."""
        wf = ashlr_server.WorkflowRun(
            id="wf1", workflow_id="w1", workflow_name="test",
            agent_specs=[], agent_map={}, pending_indices=set(),
        )
        assert wf.stage_started_at == {}

    def test_stage_timeout_sec_in_to_dict(self):
        """to_dict() should include stage_timeout_sec."""
        wf = ashlr_server.WorkflowRun(
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
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        result = manager.check_stage_timeouts()
        assert result == []

    def test_check_timeouts_not_expired(self):
        """Running workflow with recent stage_started_at should not timeout."""
        import time
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        wf = ashlr_server.WorkflowRun(
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
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        wf = ashlr_server.WorkflowRun(
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
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        wf = ashlr_server.WorkflowRun(
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
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        wf = ashlr_server.WorkflowRun(
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
        src = inspect.getsource(ashlr_server.run_workflow)
        assert "detect_circular_deps" in src

    def test_circular_dep_returns_400(self):
        """run_workflow should return 400 when circular deps found."""
        src = inspect.getsource(ashlr_server.run_workflow)
        assert "400" in src
        assert "Circular dependency" in src

    def test_stage_timeout_from_request_body(self):
        """run_workflow should accept stage_timeout_sec from request body."""
        src = inspect.getsource(ashlr_server.run_workflow)
        assert "stage_timeout_sec" in src

    def test_stage_timeout_clamped(self):
        """Stage timeout should be clamped between 60 and 7200."""
        src = inspect.getsource(ashlr_server.run_workflow)
        assert "60.0" in src or "60" in src
        assert "7200.0" in src or "7200" in src

    def test_timeout_enforcement_in_health_loop(self):
        """Health check loop should enforce stage timeouts."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "check_stage_timeouts" in src
        assert "workflow_stage_timeout" in src

    def test_stage_start_tracked_in_resolve_deps(self):
        """resolve_workflow_deps should track stage start time."""
        src = inspect.getsource(ashlr_server.AgentManager.resolve_workflow_deps)
        assert "stage_started_at" in src


# ── Resource Exhaustion Cascade Protection Tests ─────────────────────



class TestCascadeProtection:
    """Tests for resource exhaustion cascade protection features."""

    # ── Config defaults ──

    def test_config_system_cpu_pressure_threshold(self):
        """Default system CPU pressure threshold should be 90%."""
        config = ashlr_server.Config()
        assert config.system_cpu_pressure_threshold == 90.0

    def test_config_system_memory_pressure_threshold(self):
        """Default system memory pressure threshold should be 90%."""
        config = ashlr_server.Config()
        assert config.system_memory_pressure_threshold == 90.0

    def test_config_spawn_pressure_block(self):
        """Spawn pressure block should be enabled by default."""
        config = ashlr_server.Config()
        assert config.spawn_pressure_block is True

    def test_config_agent_memory_pause_pct(self):
        """Agent memory pause should default to 85% of limit."""
        config = ashlr_server.Config()
        assert config.agent_memory_pause_pct == 0.85

    def test_config_context_auto_pause_threshold(self):
        """Context auto-pause threshold should default to 0.95."""
        config = ashlr_server.Config()
        assert config.context_auto_pause_threshold == 0.95

    def test_config_pathological_error_window(self):
        """Pathological error window should default to 60s."""
        config = ashlr_server.Config()
        assert config.pathological_error_window_sec == 60.0

    def test_config_max_pathological_restarts(self):
        """Max pathological restarts should default to 1."""
        config = ashlr_server.Config()
        assert config.max_pathological_restarts == 1

    # ── Agent fields ──

    def test_agent_pathological_default_false(self):
        """Agent._pathological should default to False."""
        agent = ashlr_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._pathological is False

    def test_agent_context_auto_paused_default_false(self):
        """Agent._context_auto_paused should default to False."""
        agent = ashlr_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._context_auto_paused is False

    def test_agent_pressure_paused_default_false(self):
        """Agent._pressure_paused should default to False."""
        agent = ashlr_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent._pressure_paused is False

    # ── Per-agent CPU method ──

    def test_get_agent_cpu_exists(self):
        """AgentManager should have get_agent_cpu method."""
        assert hasattr(ashlr_server.AgentManager, 'get_agent_cpu')

    def test_get_agent_cpu_no_pid(self):
        """get_agent_cpu should return 0.0 for agent without PID."""
        import asyncio
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        agent = ashlr_server.Agent(
            id="t001", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        manager.agents["t001"] = agent
        result = asyncio.run(manager.get_agent_cpu("t001"))
        assert result == 0.0

    def test_get_agent_cpu_missing_agent(self):
        """get_agent_cpu should return 0.0 for nonexistent agent."""
        import asyncio
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        result = asyncio.run(manager.get_agent_cpu("nonexistent"))
        assert result == 0.0

    # ── CPU tracking in metrics loop ──

    def test_cpu_tracked_in_metrics_loop(self):
        """metrics_loop should update agent.cpu_pct."""
        src = inspect.getsource(ashlr_server.metrics_loop)
        assert "cpu_pct" in src
        assert "get_agent_cpu" in src

    # ── check_system_pressure ──

    def test_check_system_pressure_exists(self):
        """AgentManager should have check_system_pressure method."""
        assert hasattr(ashlr_server.AgentManager, 'check_system_pressure')

    def test_check_system_pressure_returns_dict(self):
        """check_system_pressure should return a dict with expected keys."""
        config = ashlr_server.Config()
        manager = ashlr_server.AgentManager(config)
        result = manager.check_system_pressure()
        assert isinstance(result, dict)
        assert "cpu_pressure" in result
        assert "memory_pressure" in result

    # ── Spawn pressure block ──

    def test_spawn_checks_system_pressure(self):
        """spawn() should check system pressure before spawning."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        assert "check_system_pressure" in src
        assert "spawn_pressure_block" in src

    # ── Pathological error detection ──

    def test_pathological_detection_in_health_loop(self):
        """Health check loop should detect pathological error loops."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "_pathological" in src
        assert "agent_pathological" in src
        assert "pathological_error_window_sec" in src

    def test_pathological_limits_restarts(self):
        """Health loop should limit max_restarts for pathological agents."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "max_pathological_restarts" in src

    # ── Context auto-pause ──

    def test_context_auto_pause_in_health_loop(self):
        """Health check loop should auto-pause agents at context threshold."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "context_auto_pause_threshold" in src
        assert "_context_auto_paused" in src
        assert "agent_context_auto_paused" in src

    # ── System pressure response ──

    def test_fleet_pressure_response_in_health_loop(self):
        """Health check loop should respond to system pressure."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "fleet_pressure_response" in src
        assert "_fleet_pressure_warned" in src
        assert "_pressure_paused" in src

    def test_pressure_relief_resumes_agents(self):
        """Health loop should resume pressure-paused agents when pressure relieved."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "_pressure_paused" in src
        # Check that there's logic to resume when pressure is relieved
        assert "Pressure relieved" in src or "_fleet_pressure_warned" in src

    # ── Graduated memory response ──

    def test_memory_watchdog_graduated_response(self):
        """Memory watchdog should pause before kill (graduated response)."""
        src = inspect.getsource(ashlr_server.memory_watchdog_loop)
        assert "agent_memory_paused" in src
        assert "pause_threshold" in src
        assert "agent_memory_pause_pct" in src

    def test_memory_watchdog_still_kills_at_limit(self):
        """Memory watchdog should still kill agents exceeding full limit."""
        src = inspect.getsource(ashlr_server.memory_watchdog_loop)
        assert "agent.memory_mb > limit" in src
        assert "agent_killed" in src


# ── Rate Limiting Tests ──────────────────────────────────────────────



class TestWorkflowRunGetReadyIndices:
    """Test _get_ready_indices via direct WorkflowRun state manipulation."""

    def _make_manager(self):
        """Create a minimal AgentManager mock with _get_ready_indices."""
        mgr = MagicMock()
        mgr._get_ready_indices = ashlr_server.AgentManager._get_ready_indices.__get__(mgr)
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
        mgr.on_agent_complete = ashlr_server.AgentManager.on_agent_complete.__get__(mgr)
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
        mgr.on_agent_failed = ashlr_server.AgentManager.on_agent_failed.__get__(mgr)
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
        return ashlr_server.AgentManager._safe_eval_condition(expr, ctx or {})

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



class TestEvaluateSkipIf:
    """Test AgentManager._evaluate_skip_if() branch coverage."""

    def _make_manager(self, agents=None):
        mgr = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        mgr.agents = agents or {}
        mgr._evaluate_skip_if = ashlr_server.AgentManager._evaluate_skip_if.__get__(mgr)
        mgr._safe_eval_condition = ashlr_server.AgentManager._safe_eval_condition
        mgr._resolve_skip_val = ashlr_server.AgentManager._resolve_skip_val
        return mgr

    def _make_agent(self, agent_id="dep1", status="idle", summary="All done"):
        agent = ashlr_server.Agent(
            id=agent_id, name="dep-agent", role="backend",
            status=status, task="dep task", backend="claude-code",
            working_dir="/tmp", tmux_session=f"ashlr-{agent_id}",
        )
        agent.summary = summary
        agent.created_at = ashlr_server.datetime.now(ashlr_server.timezone.utc).isoformat()
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic() - 5
        return agent

    def test_no_skip_if_returns_false(self):
        """No skip_if key on spec → should not skip."""
        specs = [{"name": "a", "role": "backend", "task": "t"}]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        assert mgr._evaluate_skip_if(wf, 0) is False

    def test_empty_skip_if_returns_false(self):
        """Empty string skip_if → falsy → should not skip."""
        specs = [{"name": "a", "role": "backend", "task": "t", "skip_if": ""}]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        assert mgr._evaluate_skip_if(wf, 0) is False

    def test_skip_if_no_depends_on_returns_false(self):
        """skip_if set but no depends_on → warning logged, returns False."""
        specs = [{"name": "a", "role": "backend", "task": "t", "skip_if": "prev.status == 'idle'"}]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        assert mgr._evaluate_skip_if(wf, 0) is False

    def test_dep_not_in_agent_map_returns_false(self):
        """Dep index not in agent_map → falls through to False."""
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2",
             "depends_on": [0], "skip_if": "prev.status == 'idle'"},
        ]
        wf = _make_wf_run(specs)
        # agent_map is empty — dep 0 not mapped
        mgr = self._make_manager()
        assert mgr._evaluate_skip_if(wf, 1) is False

    def test_dep_agent_not_in_agents_dict_returns_false(self):
        """Dep mapped in agent_map but agent killed (not in self.agents) → False."""
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2",
             "depends_on": [0], "skip_if": "prev.status == 'idle'"},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"  # mapped, but agent not in dict
        mgr = self._make_manager(agents={})
        assert mgr._evaluate_skip_if(wf, 1) is False

    def test_condition_true_skips(self):
        """Condition evaluates True → agent should be skipped."""
        dep = self._make_agent(status="idle")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2",
             "depends_on": [0], "skip_if": "prev.status == 'idle'"},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        assert mgr._evaluate_skip_if(wf, 1) is True

    def test_condition_false_does_not_skip(self):
        """Condition evaluates False → agent should not be skipped."""
        dep = self._make_agent(status="working")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2",
             "depends_on": [0], "skip_if": "prev.status == 'idle'"},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        assert mgr._evaluate_skip_if(wf, 1) is False

    def test_summary_in_condition(self):
        """'keyword' in prev.summary → skip if keyword present."""
        dep = self._make_agent(summary="All tests passed successfully")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2",
             "depends_on": [0], "skip_if": "'passed' in prev.summary"},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        assert mgr._evaluate_skip_if(wf, 1) is True

    def test_first_dep_wins_semantics(self):
        """With multiple deps, first resolvable dep is used."""
        dep0 = self._make_agent(agent_id="d0", status="idle")
        dep1 = self._make_agent(agent_id="d1", status="error")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t"},
            {"name": "c", "role": "tester", "task": "t",
             "depends_on": [0, 1], "skip_if": "prev.status == 'idle'"},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "d0"
        wf.agent_map[1] = "d1"
        mgr = self._make_manager(agents={"d0": dep0, "d1": dep1})
        # First dep (d0) is idle → condition True → skip
        assert mgr._evaluate_skip_if(wf, 2) is True

    def test_first_dep_unresolvable_uses_second(self):
        """First dep not in agent_map, second is resolvable."""
        dep1 = self._make_agent(agent_id="d1", status="error")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t"},
            {"name": "c", "role": "tester", "task": "t",
             "depends_on": [0, 1], "skip_if": "prev.status == 'error'"},
        ]
        wf = _make_wf_run(specs)
        # dep 0 not in agent_map, dep 1 is mapped
        wf.agent_map[1] = "d1"
        mgr = self._make_manager(agents={"d1": dep1})
        assert mgr._evaluate_skip_if(wf, 2) is True


# ─────────────────────────────────────────────
# T29: _build_dep_context — predecessor context builder
# ─────────────────────────────────────────────



class TestBuildDepContext:
    """Test AgentManager._build_dep_context() branch coverage."""

    def _make_manager(self, agents=None, file_activity=None):
        mgr = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        mgr.agents = agents or {}
        mgr.file_activity = file_activity or {}
        mgr._build_dep_context = ashlr_server.AgentManager._build_dep_context.__get__(mgr)
        return mgr

    def _make_agent(self, agent_id="dep1", name="dep-agent", role="backend",
                    summary="Finished building API", output_lines=None):
        agent = ashlr_server.Agent(
            id=agent_id, name=name, role=role,
            status="idle", task="Build the API", backend="claude-code",
            working_dir="/tmp", tmux_session=f"ashlr-{agent_id}",
        )
        agent.summary = summary
        agent.created_at = ashlr_server.datetime.now(ashlr_server.timezone.utc).isoformat()
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic() - 5
        if output_lines:
            agent.output_lines.extend(output_lines)
        return agent

    def test_no_depends_on_returns_empty(self):
        """Spec with no depends_on → empty string."""
        specs = [{"name": "a", "role": "backend", "task": "t"}]
        wf = _make_wf_run(specs)
        mgr = self._make_manager()
        assert mgr._build_dep_context(wf, 0) == ""

    def test_dep_not_in_agent_map_skipped(self):
        """Dep index not in agent_map → silently skipped."""
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        # agent_map is empty
        mgr = self._make_manager()
        result = mgr._build_dep_context(wf, 1)
        # Header is added but no agent sections
        assert "Context from predecessor" in result
        assert "###" not in result

    def test_dep_agent_killed_skipped(self):
        """Dep mapped but agent removed from self.agents → skipped."""
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={})  # dep1 not in agents
        result = mgr._build_dep_context(wf, 1)
        assert "###" not in result

    def test_full_section_with_agent(self):
        """Fully resolved dep → name, task, summary in output."""
        dep = self._make_agent()
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        result = mgr._build_dep_context(wf, 1)
        assert "### Agent 'dep-agent'" in result
        assert "Task: Build the API" in result
        assert "Summary: Finished building API" in result

    def test_files_touched_included(self):
        """File activity referencing the dep agent → Files touched line appears."""
        dep = self._make_agent()
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        file_activity = {
            "src/app.ts": {"dep1": "write"},
            "src/utils.ts": {"dep1": "read"},
            "README.md": {"other": "write"},
        }
        mgr = self._make_manager(agents={"dep1": dep}, file_activity=file_activity)
        result = mgr._build_dep_context(wf, 1)
        assert "Files touched:" in result
        assert "src/app.ts" in result
        assert "src/utils.ts" in result
        assert "README.md" not in result  # belongs to other agent

    def test_no_files_no_files_touched_line(self):
        """No file activity for dep → Files touched line absent."""
        dep = self._make_agent()
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        result = mgr._build_dep_context(wf, 1)
        assert "Files touched:" not in result

    def test_substantive_output_included(self):
        """Agent with substantive output lines → Recent output block appears."""
        dep = self._make_agent(output_lines=[
            "Reading src/app.ts...",
            "Writing src/handler.ts...",
            "All tests passed: 15 passed, 0 failed",
        ])
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        result = mgr._build_dep_context(wf, 1)
        assert "Recent output:" in result
        assert "All tests passed" in result

    def test_noise_output_filtered(self):
        """Short/noise lines filtered; Recent output block absent if all noise."""
        dep = self._make_agent(output_lines=[
            "  ",
            "ab",
            "███░░░",
            "⠋⠙⠹",
        ])
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        result = mgr._build_dep_context(wf, 1)
        assert "Recent output:" not in result

    def test_ansi_stripped_from_output(self):
        """ANSI escape codes stripped from output lines."""
        dep = self._make_agent(output_lines=[
            "\x1b[32mWriting src/handler.ts\x1b[0m",
        ])
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        mgr = self._make_manager(agents={"dep1": dep})
        result = mgr._build_dep_context(wf, 1)
        assert "Recent output:" in result
        assert "\x1b[" not in result
        assert "Writing src/handler.ts" in result

    def test_multiple_deps_both_sections(self):
        """Multiple deps resolved → both produce sections."""
        dep0 = self._make_agent(agent_id="d0", name="api-builder")
        dep1 = self._make_agent(agent_id="d1", name="test-runner")
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "tester", "task": "t"},
            {"name": "c", "role": "reviewer", "task": "t", "depends_on": [0, 1]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "d0"
        wf.agent_map[1] = "d1"
        mgr = self._make_manager(agents={"d0": dep0, "d1": dep1})
        result = mgr._build_dep_context(wf, 2)
        assert "api-builder" in result
        assert "test-runner" in result

    def test_files_capped_at_20(self):
        """More than 20 files → only first 20 shown."""
        dep = self._make_agent()
        specs = [
            {"name": "a", "role": "backend", "task": "t"},
            {"name": "b", "role": "frontend", "task": "t2", "depends_on": [0]},
        ]
        wf = _make_wf_run(specs)
        wf.agent_map[0] = "dep1"
        file_activity = {f"src/file{i}.ts": {"dep1": "write"} for i in range(25)}
        mgr = self._make_manager(agents={"dep1": dep}, file_activity=file_activity)
        result = mgr._build_dep_context(wf, 1)
        # Count file references in Files touched line
        files_line = [l for l in result.split("\n") if "Files touched:" in l][0]
        # Should have at most 20 files (comma-separated)
        file_count = len(files_line.split("Files touched: ")[1].split(", "))
        assert file_count == 20


# ── Database close and init safety ──



class TestWorkflowValidation:
    """Tests for _validate_workflow_specs shared validation helper."""

    def test_self_dependency_rejected(self):
        specs = [{"role": "backend", "depends_on": [0]}]
        error = ashlr_server._validate_workflow_specs(specs)
        assert error is not None
        assert "cannot reference itself" in error

    def test_circular_dependency_detected(self):
        specs = [
            {"role": "backend", "depends_on": [1]},
            {"role": "frontend", "depends_on": [0]},
        ]
        error = ashlr_server._validate_workflow_specs(specs)
        assert error is not None
        assert "Circular dependency" in error

    def test_valid_workflow_passes(self):
        specs = [
            {"role": "backend"},
            {"role": "frontend", "depends_on": [0]},
            {"role": "tester", "depends_on": [0, 1]},
        ]
        error = ashlr_server._validate_workflow_specs(specs)
        assert error is None


# ─────────────────────────────────────────────
# T-NEW: _safe_commit return value
# ─────────────────────────────────────────────


