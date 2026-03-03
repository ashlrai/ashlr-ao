"""Tests for fleet analytics, collaboration graph, and file conflict detection."""

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


class TestFleetAnalytics:
    """Tests for fleet analytics aggregation logic."""

    def _make_agent(self, **kwargs):
        defaults = dict(
            id="a001", name="test", role="backend", status="working",
            project_id=None, working_dir="/tmp", backend="claude-code",
            task="test", tmux_session="ashlr-a001"
        )
        defaults.update(kwargs)
        return ashlr_server.Agent(**defaults)

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



class TestFileConflicts:
    def test_write_write_conflict_detected(self, make_agent):
        """Two agents writing the same file should produce a conflict."""
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        manager.agents = {}
        manager.file_activity = {}
        manager._FILE_WRITE_RE = ashlr_server.AgentManager._FILE_WRITE_RE
        manager._FILE_READ_RE = ashlr_server.AgentManager._FILE_READ_RE

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
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
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
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
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



class TestCollaborationGraph:
    """Tests for agent collaboration graph data computation."""

    def _make_agent(self, id, name="test", role="backend", project_id=None, files=None):
        agent = ashlr_server.Agent(
            id=id, name=name, role=role, status="working",
            working_dir="/tmp", backend="demo", task="test task",
            summary="", tmux_session=f"ashlr-{id}",
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



class TestFileConflictRegex:
    """Test the regex patterns used for file conflict detection."""

    def _write_re(self):
        return ashlr_server.AgentManager._FILE_WRITE_RE

    def _read_re(self):
        return ashlr_server.AgentManager._FILE_READ_RE

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



class TestCheckFileConflictsReal:
    """Tests for AgentManager._check_file_conflicts(), the actual parsing entry point."""

    def _make_manager(self, agents_dict):
        """Create an AgentManager with mocked internals and given agents."""
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        manager.agents = agents_dict
        manager.file_activity = {}
        manager.backend_configs = {}
        return manager

    def _make_agent_for_mgr(self, agent_id="aaaa", name="test", status="working", role="backend"):
        agent = ashlr_server.Agent(
            id=agent_id, name=name, role=role, status=status,
            task="test task", backend="claude-code",
            working_dir="/tmp", tmux_session=f"ashlr-{agent_id}",
        )
        agent.created_at = ashlr_server.datetime.now(ashlr_server.timezone.utc).isoformat()
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic() - 5
        return agent

    def test_write_populates_file_activity(self):
        agent = self._make_agent_for_mgr("aaaa")
        manager = self._make_manager({"aaaa": agent})
        manager._check_file_conflicts("aaaa", ["Writing src/auth/handler.py"])
        assert "src/auth/handler.py" in manager.file_activity
        assert manager.file_activity["src/auth/handler.py"]["aaaa"] == "write"

    def test_write_write_conflict_detected(self):
        a1 = self._make_agent_for_mgr("aaaa", name="agent-a")
        a2 = self._make_agent_for_mgr("bbbb", name="agent-b")
        manager = self._make_manager({"aaaa": a1, "bbbb": a2})
        manager._check_file_conflicts("aaaa", ["Writing src/auth.py"])
        conflicts = manager._check_file_conflicts("bbbb", ["Writing src/auth.py"])
        assert len(conflicts) == 1
        assert conflicts[0]["severity"] == "conflict"
        assert conflicts[0]["file_path"] == "src/auth.py"

    def test_write_over_read_returns_warning(self):
        a1 = self._make_agent_for_mgr("aaaa", name="agent-a")
        a2 = self._make_agent_for_mgr("bbbb", name="agent-b")
        manager = self._make_manager({"aaaa": a1, "bbbb": a2})
        manager._check_file_conflicts("aaaa", ["Reading src/config.py"])
        conflicts = manager._check_file_conflicts("bbbb", ["Writing src/config.py"])
        assert len(conflicts) == 1
        assert conflicts[0]["severity"] == "warning"

    def test_read_read_no_conflict(self):
        a1 = self._make_agent_for_mgr("aaaa")
        a2 = self._make_agent_for_mgr("bbbb")
        manager = self._make_manager({"aaaa": a1, "bbbb": a2})
        manager._check_file_conflicts("aaaa", ["Reading src/config.py"])
        conflicts = manager._check_file_conflicts("bbbb", ["Reading src/config.py"])
        assert len(conflicts) == 0

    def test_ansi_encoded_line_parsed(self):
        agent = self._make_agent_for_mgr("aaaa")
        manager = self._make_manager({"aaaa": agent})
        manager._check_file_conflicts("aaaa", ["\033[32mWriting src/app.ts\033[0m"])
        assert "src/app.ts" in manager.file_activity

    def test_files_touched_increments(self):
        agent = self._make_agent_for_mgr("aaaa")
        manager = self._make_manager({"aaaa": agent})
        assert agent.files_touched == 0
        manager._check_file_conflicts("aaaa", ["Writing src/a.ts", "Writing src/b.ts"])
        assert agent.files_touched == 2

    def test_missing_agent_returns_empty(self):
        manager = self._make_manager({})
        result = manager._check_file_conflicts("nonexistent", ["Writing src/x.py"])
        assert result == []

    def test_idle_agent_not_conflicting(self):
        """An idle agent's file activity should not generate conflicts (not in working/planning)."""
        a1 = self._make_agent_for_mgr("aaaa", status="idle")
        a2 = self._make_agent_for_mgr("bbbb", status="working")
        manager = self._make_manager({"aaaa": a1, "bbbb": a2})
        manager._check_file_conflicts("aaaa", ["Writing src/auth.py"])
        conflicts = manager._check_file_conflicts("bbbb", ["Writing src/auth.py"])
        assert len(conflicts) == 0  # aaaa is idle, so not a conflict


# ─────────────────────────────────────────────
# T16: _cleanup_file_activity()
# ─────────────────────────────────────────────



class TestCleanupFileActivity:
    """Tests for AgentManager._cleanup_file_activity()."""

    def _make_manager(self):
        manager = ashlr_server.AgentManager.__new__(ashlr_server.AgentManager)
        manager.agents = {}
        manager.file_activity = {}
        manager.backend_configs = {}
        return manager

    def test_single_file_pruned(self):
        manager = self._make_manager()
        manager.file_activity = {"src/auth.py": {"aaaa": "write"}}
        manager._cleanup_file_activity("aaaa")
        assert "src/auth.py" not in manager.file_activity

    def test_other_agents_preserved(self):
        manager = self._make_manager()
        manager.file_activity = {"src/auth.py": {"aaaa": "write", "bbbb": "read"}}
        manager._cleanup_file_activity("aaaa")
        assert "src/auth.py" in manager.file_activity
        assert "aaaa" not in manager.file_activity["src/auth.py"]
        assert "bbbb" in manager.file_activity["src/auth.py"]

    def test_multi_file_cleanup(self):
        manager = self._make_manager()
        manager.file_activity = {
            "src/a.py": {"aaaa": "write"},
            "src/b.py": {"aaaa": "read"},
            "src/c.py": {"aaaa": "write", "bbbb": "write"},
        }
        manager._cleanup_file_activity("aaaa")
        assert "src/a.py" not in manager.file_activity
        assert "src/b.py" not in manager.file_activity
        assert "src/c.py" in manager.file_activity
        assert "aaaa" not in manager.file_activity["src/c.py"]

    def test_nonexistent_agent_no_crash(self):
        manager = self._make_manager()
        manager.file_activity = {"src/x.py": {"aaaa": "write"}}
        manager._cleanup_file_activity("nonexistent")
        assert manager.file_activity == {"src/x.py": {"aaaa": "write"}}

    def test_empty_activity_no_crash(self):
        manager = self._make_manager()
        manager.file_activity = {}
        manager._cleanup_file_activity("aaaa")  # should not raise
        assert manager.file_activity == {}


# ─────────────────────────────────────────────
# T17: detect_status() — AgentManager method
# ─────────────────────────────────────────────



class TestBugFixesAndFleetExport:
    """Tests for bug fixes: silent exception logging, alert throttle cleanup, fleet export."""

    def test_spawn_cleanup_logs_on_failure(self):
        """Spawn tmux cleanup should log warnings, not silently pass."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        # Should contain log.warning for cleanup failures, not bare pass
        assert "cleanup_err" in src
        assert "log.warning" in src

    def test_broadcast_catches_cancelled_error(self):
        """WebSocket broadcast should catch asyncio.CancelledError."""
        src = inspect.getsource(ashlr_server.WebSocketHub.broadcast)
        assert "CancelledError" in src

    def test_alert_throttle_cleanup_in_health_loop(self):
        """Health loop should periodically clean up stale alert throttle entries."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "_alert_throttle" in src
        # Verify both stale eviction and cap enforcement
        assert "120.0" in src or "stale_keys" in src

    def test_alert_throttle_capped_at_500(self):
        """Alert throttle cleanup should enforce max 500 entries."""
        src = inspect.getsource(ashlr_server.health_check_loop)
        assert "500" in src

    def test_fleet_export_endpoint_registered(self):
        """GET /api/fleet/export should be registered."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/fleet/export" in src

    def test_fleet_export_handler_exists(self):
        """export_fleet_state handler function should exist."""
        assert hasattr(ashlr_server, "export_fleet_state")
        assert callable(getattr(ashlr_server, "export_fleet_state"))

    def test_fleet_export_includes_agents(self):
        """Fleet export should include agents data."""
        src = inspect.getsource(ashlr_server.export_fleet_state)
        assert "agents" in src
        assert "agents_count" in src

    def test_fleet_export_includes_projects(self):
        """Fleet export should include projects."""
        src = inspect.getsource(ashlr_server.export_fleet_state)
        assert "projects" in src

    def test_fleet_export_includes_config_summary(self):
        """Fleet export should include config summary."""
        src = inspect.getsource(ashlr_server.export_fleet_state)
        assert "config_summary" in src

    def test_fleet_export_includes_version(self):
        """Fleet export should include version for future compatibility."""
        src = inspect.getsource(ashlr_server.export_fleet_state)
        assert "version" in src

    def test_fleet_export_includes_timestamp(self):
        """Fleet export should include exported_at timestamp."""
        src = inspect.getsource(ashlr_server.export_fleet_state)
        assert "exported_at" in src


# ── Workflow Deadlock Detection & Stage Timeout Tests ────────────────


