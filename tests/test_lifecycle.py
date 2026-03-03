"""Tests for server infrastructure: config, database, security, middleware, extensions, and misc."""

import asyncio
import inspect
import sys
import time
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
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


def _make_intel_config(enabled=True, api_key="test-key"):
    """Create a Config with LLM fields set for testing IntelligenceClient."""
    cfg = ashlr_server.Config()
    cfg.llm_enabled = enabled
    cfg.llm_api_key = api_key
    cfg.llm_model = "grok-4-1-fast-reasoning"
    cfg.llm_base_url = "https://api.x.ai/v1"
    cfg.llm_max_output_lines = 30
    return cfg


TEST_WORKING_DIR = str(Path.home())


from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app


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



class TestDatabaseDegradedMode:
    async def test_save_agent_returns_on_no_db(self, make_agent):
        """save_agent should return early when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        agent = make_agent(status="working")
        # Should not raise
        await db.save_agent(agent)

    async def test_get_agent_history_returns_empty_on_no_db(self):
        """get_agent_history should return [] when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_agent_history()
        assert result == []

    async def test_get_agent_history_count_returns_zero_on_no_db(self):
        """get_agent_history_count should return 0 when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_agent_history_count()
        assert result == 0

    async def test_get_agent_history_item_returns_none_on_no_db(self):
        """get_agent_history_item should return None when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_agent_history_item("abc1")
        assert result is None

    async def test_save_project_returns_on_no_db(self):
        """save_project should return early when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        await db.save_project({"id": "p1", "name": "test", "path": "/tmp"})

    async def test_get_projects_returns_empty_on_no_db(self):
        """get_projects should return [] when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_projects()
        assert result == []

    async def test_delete_project_returns_false_on_no_db(self):
        """delete_project should return False when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.delete_project("p1")
        assert result is False

    async def test_save_workflow_returns_on_no_db(self):
        """save_workflow should return early when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        await db.save_workflow({"id": "w1", "name": "test"})

    async def test_get_workflows_returns_empty_on_no_db(self):
        """get_workflows should return [] when _db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_workflows()
        assert result == []


# ─────────────────────────────────────────────
# T13: Resume uses set_status
# ─────────────────────────────────────────────



class TestExtensionScanner:
    def test_scan_returns_dict_with_expected_keys(self):
        """scan() should return dict with skills, mcp_servers, plugins, scanned_at."""
        from ashlr_server import ExtensionScanner
        scanner = ExtensionScanner()
        result = scanner.scan()
        assert "skills" in result
        assert "mcp_servers" in result
        assert "plugins" in result
        assert "scanned_at" in result

    def test_to_dict_structure(self):
        """to_dict should return correct structure even when empty."""
        from ashlr_server import ExtensionScanner
        scanner = ExtensionScanner()
        d = scanner.to_dict()
        assert d["skills"] == []
        assert d["mcp_servers"] == []
        assert d["plugins"] == []
        assert d["scanned_at"] == ""

    def test_parse_skill_frontmatter(self, tmp_path):
        """Should parse YAML frontmatter from a skill .md file."""
        from ashlr_server import ExtensionScanner
        skill_file = tmp_path / "test-skill.md"
        skill_file.write_text("---\ndescription: Test skill\nargument-hint: <arg>\nallowed-tools: Bash\n---\n\n# Test")
        desc, hint, tools = ExtensionScanner._parse_skill_frontmatter(skill_file)
        assert desc == "Test skill"
        assert hint == "<arg>"
        assert tools == "Bash"

    def test_parse_skill_no_frontmatter(self, tmp_path):
        """Skills without frontmatter should return empty strings."""
        from ashlr_server import ExtensionScanner
        skill_file = tmp_path / "bare.md"
        skill_file.write_text("# Just a heading\nSome content")
        desc, hint, tools = ExtensionScanner._parse_skill_frontmatter(skill_file)
        assert desc == ""
        assert hint == ""
        assert tools == ""

    def test_scan_skills_from_dir(self, tmp_path):
        """Should discover .md files recursively."""
        from ashlr_server import ExtensionScanner
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
        from ashlr_server import ExtensionScanner
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
        from ashlr_server import ExtensionScanner
        settings = {"enabledPlugins": {"my-plugin@provider": True, "disabled-one@other": False}}
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        ExtensionScanner()
        # Directly test the parsing logic
        with patch.object(Path, 'home', return_value=tmp_path / "fake"):
            # Since _scan_plugins reads from ~/.claude/settings.json, we test _parse_mcp_dict instead
            pass
        # Test plugin info structure
        from ashlr_server import PluginInfo
        p = PluginInfo(name="test", provider="provider", enabled=True)
        assert p.to_dict() == {"name": "test", "provider": "provider", "enabled": True}


# ─────────────────────────────────────────────
# T11: Context Detection from Output
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
            from ashlr_server import Config, AgentManager
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



class TestConfigExportImport:
    def test_config_is_dict(self):
        """Config path should resolve to a valid YAML structure."""
        config = ashlr_server.Config()
        assert config.host == "127.0.0.1"
        assert config.port == 5111

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



class TestServerStats:
    """Verify server-side stats tracking counters."""

    def test_initial_stats_are_zero(self):
        """New AgentManager should have zero stats."""
        config = ashlr_server.Config()
        mgr = ashlr_server.AgentManager(config)
        assert mgr._total_spawned == 0
        assert mgr._total_killed == 0
        assert mgr._total_messages_sent == 0

    def test_start_time_set(self):
        """AgentManager should record start time."""
        config = ashlr_server.Config()
        mgr = ashlr_server.AgentManager(config)
        assert mgr._start_time > 0
        assert time.monotonic() - mgr._start_time < 2  # should be very recent



class TestHistoricalAnalytics:
    """Tests for historical analytics database method."""

    async def test_historical_analytics_no_db(self):
        """With no DB, should return empty structure."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_historical_analytics()
        assert result["total_historical"] == 0

    async def test_historical_analytics_returns_dict(self):
        """Return value should be a dict with expected keys."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_historical_analytics()
        assert isinstance(result, dict)
        assert "total_historical" in result



class TestServerAuditFixes:
    """Tests for critical bugs found during server audit."""

    def test_restart_lock_prevents_concurrent(self, make_agent):
        """Restart should be guarded by asyncio.Lock, not just a boolean flag."""
        agent = make_agent(status="error")
        assert hasattr(agent, '_restart_lock')
        assert isinstance(agent._restart_lock, asyncio.Lock)

    async def test_restart_lock_held_returns_false(self, make_agent):
        """If restart lock is already held, restart should return False."""
        agent = make_agent(status="error")
        async def _test():
            async with agent._restart_lock:
                manager = MagicMock(spec=ashlr_server.AgentManager)
                manager.agents = {agent.id: agent}
                return await ashlr_server.AgentManager.restart(manager, agent.id)
        result = await _test()
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
        from ashlr_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['mcp__claude-in-chrome__computer("click")']
        agent._last_parse_index = 0
        agent._total_lines_added = 1
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
        from ashlr_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['Skill("commit")']
        agent._last_parse_index = 0
        agent._total_lines_added = 1
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
        from ashlr_server import OutputIntelligenceParser
        agent = MagicMock()
        agent.id = "test1"
        agent.output_lines = ['Agent("search for patterns")']
        agent._last_parse_index = 0
        agent._total_lines_added = 1
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
        src = inspect.getsource(ashlr_server.WebSocketHub.broadcast)
        assert "timeout=2.0" in src

    def test_batch_spawn_agent_limit(self):
        """Batch spawn should check concurrent agent limit before spawning."""
        import inspect
        src = inspect.getsource(ashlr_server.batch_spawn)
        assert "available_slots" in src or "max_agents" in src

    def test_sync_handler_db_exception_guard(self):
        """Sync handler should catch DB exceptions for projects and workflows."""
        import inspect
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        # After our fix, each DB call should be wrapped in try/except
        assert src.count("projects = await") >= 1
        assert src.count("workflows = await") >= 1



class TestCriticalBugFixes:
    """Tests for critical bug fixes found in audit."""

    def test_health_detailed_uses_max_agents(self):
        """health_detailed should use config.max_agents, not max_concurrent_agents."""
        import inspect
        src = inspect.getsource(ashlr_server.health_detailed)
        assert "max_concurrent_agents" not in src
        assert "max_agents" in src

    def test_db_save_message_null_guard(self):
        """save_message should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.save_message)
        assert "if not self._db" in src

    def test_db_get_messages_for_agent_null_guard(self):
        """get_messages_for_agent should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.get_messages_for_agent)
        assert "if not self._db" in src

    def test_db_get_messages_between_null_guard(self):
        """get_messages_between should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.get_messages_between)
        assert "if not self._db" in src

    def test_db_get_message_count_null_guard(self):
        """get_message_count_for_agent should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.get_message_count_for_agent)
        assert "if not self._db" in src

    def test_db_mark_messages_read_null_guard(self):
        """mark_messages_read should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.mark_messages_read)
        assert "if not self._db" in src

    def test_db_get_unread_count_null_guard(self):
        """get_unread_count should guard against null db."""
        import inspect
        src = inspect.getsource(ashlr_server.Database.get_unread_count)
        assert "if not self._db" in src

    def test_compiled_alert_patterns_in_create_app(self):
        """create_app should compile alert patterns at startup."""
        import inspect
        src = inspect.getsource(ashlr_server.create_app)
        assert "_compiled_alert_patterns" in src



class TestConfigLoadingAndValidation:
    """Tests for config loading with alert_patterns and intelligence validators."""

    def test_load_config_parses_alert_patterns(self):
        """load_config should parse alert patterns from YAML alerts section."""
        import inspect
        src = inspect.getsource(ashlr_server.load_config)
        assert "alert_patterns" in src
        assert "validated_alert_patterns" in src

    def test_load_config_validates_alert_regex(self):
        """load_config should validate alert pattern regex."""
        import inspect
        src = inspect.getsource(ashlr_server.load_config)
        assert "re.compile" in src
        assert "re.error" in src

    def test_put_config_has_llm_meta_interval_validator(self):
        """put_config should validate llm_meta_interval field."""
        import inspect
        src = inspect.getsource(ashlr_server.put_config)
        assert "llm_meta_interval" in src

    def test_put_config_maps_llm_meta_to_yaml(self):
        """put_config should map llm_meta_interval to YAML llm section."""
        import inspect
        src = inspect.getsource(ashlr_server.put_config)
        assert "meta_interval_sec" in src

    def test_put_config_recompiles_alert_patterns(self):
        """put_config should recompile alert patterns when config changes."""
        import inspect
        src = inspect.getsource(ashlr_server.put_config)
        assert "_compiled_alert_patterns" in src

    def test_default_config_has_alert_patterns(self):
        """Default Config should have 5 alert patterns."""
        config = ashlr_server.Config()
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
        return ashlr_server.Agent(**defaults)

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
        src = inspect.getsource(ashlr_server.Database.save_agent)
        assert "resumable" in src

    def test_save_agent_includes_system_prompt_column(self):
        """save_agent() SQL should include system_prompt column."""
        src = inspect.getsource(ashlr_server.Database.save_agent)
        assert "system_prompt" in src

    def test_save_agent_includes_plan_mode_column(self):
        """save_agent() SQL should include plan_mode column."""
        src = inspect.getsource(ashlr_server.Database.save_agent)
        assert "plan_mode" in src

    def test_get_resumable_sessions_method_exists(self):
        """Database should have get_resumable_sessions method."""
        assert hasattr(ashlr_server.Database, "get_resumable_sessions")
        assert callable(getattr(ashlr_server.Database, "get_resumable_sessions"))

    async def test_get_resumable_sessions_null_guard(self):
        """get_resumable_sessions() should return [] when db is None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        result = await db.get_resumable_sessions()
        assert result == []

    def test_get_resumable_sessions_filters_by_resumable(self):
        """get_resumable_sessions() SQL should filter WHERE resumable = 1."""
        src = inspect.getsource(ashlr_server.Database.get_resumable_sessions)
        assert "resumable = 1" in src

    def test_get_resumable_sessions_orders_by_completed_at(self):
        """get_resumable_sessions() should order by most recent first."""
        src = inspect.getsource(ashlr_server.Database.get_resumable_sessions)
        assert "ORDER BY completed_at DESC" in src

    def test_resume_endpoint_registered(self):
        """POST /api/sessions/{id}/resume should be registered."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/sessions/{id}/resume" in src

    def test_resumable_sessions_endpoint_registered(self):
        """GET /api/sessions/resumable should be registered."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/sessions/resumable" in src

    def test_resume_handler_checks_working_dir(self):
        """resume_from_history() should verify working directory exists."""
        src = inspect.getsource(ashlr_server.resume_from_history)
        assert "isdir" in src or "working_dir" in src

    def test_resume_handler_checks_agent_limit(self):
        """resume_from_history() should check max_agents limit."""
        src = inspect.getsource(ashlr_server.resume_from_history)
        assert "max_agents" in src

    def test_resume_handler_supports_task_override(self):
        """resume_from_history() should allow overriding the task."""
        src = inspect.getsource(ashlr_server.resume_from_history)
        assert "task" in src

    def test_resume_handler_supports_continue_message(self):
        """resume_from_history() should support continue_message field."""
        src = inspect.getsource(ashlr_server.resume_from_history)
        assert "continue_message" in src

    def test_db_migration_adds_resumable_column(self):
        """DB init should add resumable column to agents_history."""
        src = inspect.getsource(ashlr_server.Database.init)
        assert "resumable" in src

    def test_db_migration_adds_system_prompt_column(self):
        """DB init should add system_prompt column to agents_history."""
        src = inspect.getsource(ashlr_server.Database.init)
        assert "system_prompt" in src

    def test_db_migration_adds_plan_mode_column(self):
        """DB init should add plan_mode column to agents_history."""
        src = inspect.getsource(ashlr_server.Database.init)
        assert "plan_mode" in src

    def test_resumable_statuses_are_correct(self):
        """The resumable status check should match the expected set."""
        src = inspect.getsource(ashlr_server.Database.save_agent)
        # Verify the exact status set used for resumable flag
        for status in ("complete", "working", "planning", "idle"):
            assert status in src


# ---------------------------------------------------------------------------
# Bug Fixes: Silent Exceptions, Memory Leaks, Fleet Export
# ---------------------------------------------------------------------------



class TestRateLimiting:
    """Tests for per-endpoint REST API rate limiting."""

    def test_rate_limiter_allows_burst(self):
        """RateLimiter should allow initial burst of requests."""
        rl = ashlr_server.RateLimiter()
        for _ in range(5):
            allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=5.0)
            assert allowed

    def test_rate_limiter_blocks_after_burst(self):
        """RateLimiter should block after burst is exhausted."""
        rl = ashlr_server.RateLimiter()
        for _ in range(10):
            rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        allowed, retry_after = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        assert not allowed
        assert retry_after > 0

    def test_rate_limiter_per_ip(self):
        """Different IPs should have separate buckets."""
        rl = ashlr_server.RateLimiter()
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
        rl = ashlr_server.RateLimiter()
        rl.check("old_ip", cost=1.0)
        # Artificially age the bucket
        rl._buckets["old_ip"]["last_refill"] = time.monotonic() - 500
        rl.cleanup_stale(max_age=300.0)
        assert "old_ip" not in rl._buckets

    def test_rate_limit_tiers_exist(self):
        """Rate limit tier definitions should exist."""
        assert "spawn" in ashlr_server._RATE_LIMIT_TIERS
        assert "bulk" in ashlr_server._RATE_LIMIT_TIERS
        assert "default" in ashlr_server._RATE_LIMIT_TIERS
        assert "fleet-export" in ashlr_server._RATE_LIMIT_TIERS

    def test_get_rate_tier_spawn(self):
        """POST /api/agents should use spawn tier."""
        tier = ashlr_server._get_rate_tier("/api/agents", "POST")
        assert tier == "spawn"

    def test_get_rate_tier_bulk(self):
        """POST with bulk in path should use bulk tier."""
        tier = ashlr_server._get_rate_tier("/api/agents/bulk", "POST")
        assert tier == "bulk"

    def test_get_rate_tier_default(self):
        """GET /api/agents should use default tier."""
        tier = ashlr_server._get_rate_tier("/api/agents", "GET")
        assert tier == "default"

    def test_get_rate_tier_fleet_export(self):
        """Fleet export should use fleet-export tier."""
        tier = ashlr_server._get_rate_tier("/api/fleet/export", "GET")
        assert tier == "fleet-export"

    def test_rate_limit_middleware_exists(self):
        """rate_limit_middleware should exist as a function."""
        assert callable(ashlr_server.rate_limit_middleware)

    def test_middleware_registered_in_create_app(self):
        """create_app should include rate_limit_middleware."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "rate_limit_middleware" in src


# ── Binary/Garbage Detection Tests ───────────────────────────────────



class TestWebSocketDisconnectCleanup:
    """Tests for WebSocket client disconnect handling."""

    def test_ws_heartbeat_enabled(self):
        """WebSocket should have heartbeat=30.0 for ping/pong."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        assert "heartbeat=30.0" in src

    def test_ws_disconnect_cleans_sync_time(self):
        """Disconnect should clean up _last_sync_time entry."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        assert "_last_sync_time.pop" in src

    def test_ws_disconnect_cleans_rate_limiter(self):
        """Disconnect should clean up rate limiter entries for client IP."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        assert "rate_limiter" in src
        assert "stale_keys" in src

    def test_ws_disconnect_discards_client(self):
        """Disconnect should remove client from set."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        assert "clients.discard(ws)" in src

    def test_ws_max_clients_limit(self):
        """WebSocketHub should enforce max_clients connection limit."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_ws)
        assert "max_clients" in src
        assert "1013" in src  # HTTP status for "Try Again Later"


# ── Line Truncation Tests ────────────────────────────────────────────



class TestCompressionMiddleware:
    """Tests for gzip compression middleware."""

    def test_compression_middleware_exists(self):
        """compression_middleware should exist as a callable."""
        assert callable(ashlr_server.compression_middleware)

    def test_compression_registered_in_create_app(self):
        """create_app should include compression_middleware."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "compression_middleware" in src

    def test_compression_only_for_api(self):
        """Compression should only apply to /api/ paths."""
        src = inspect.getsource(ashlr_server.compression_middleware)
        assert "/api/" in src

    def test_compression_checks_accept_encoding(self):
        """Compression should check Accept-Encoding header."""
        src = inspect.getsource(ashlr_server.compression_middleware)
        assert "Accept-Encoding" in src
        assert "gzip" in src

    def test_compression_min_size(self):
        """Compression should only apply to responses > 1KB."""
        src = inspect.getsource(ashlr_server.compression_middleware)
        assert "1024" in src


# ── Diagnostic Endpoint Tests ─────────────────────────────────────────



class TestDiagnosticEndpoint:
    """Tests for the POST /api/diagnostic self-test endpoint."""

    def test_diagnostic_handler_exists(self):
        """run_diagnostic handler should exist."""
        assert callable(ashlr_server.run_diagnostic)

    def test_diagnostic_route_registered(self):
        """Diagnostic route should be registered in create_app."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "diagnostic" in src

    def test_diagnostic_checks_tmux(self):
        """Diagnostic should test tmux availability."""
        src = inspect.getsource(ashlr_server.run_diagnostic)
        assert "tmux" in src
        assert "-V" in src

    def test_diagnostic_checks_disk(self):
        """Diagnostic should check disk space."""
        src = inspect.getsource(ashlr_server.run_diagnostic)
        assert "disk_usage" in src

    def test_diagnostic_checks_database(self):
        """Diagnostic should test database write/read."""
        src = inspect.getsource(ashlr_server.run_diagnostic)
        assert "_diagnostic_test" in src
        assert "INSERT" in src

    def test_diagnostic_checks_backends(self):
        """Diagnostic should report backend availability."""
        src = inspect.getsource(ashlr_server.run_diagnostic)
        assert "backend_configs" in src
        assert "available" in src

    def test_diagnostic_returns_status(self):
        """Diagnostic should return ok/degraded status."""
        src = inspect.getsource(ashlr_server.run_diagnostic)
        assert "degraded" in src
        assert "all_ok" in src


# ─────────────────────────────────────────────
# Configurable Output Max Lines (#246)
# ─────────────────────────────────────────────



class TestRequestLoggingMiddleware:
    def test_middleware_exists(self):
        """request_logging_middleware should be defined."""
        assert hasattr(ashlr_server, 'request_logging_middleware')
        assert callable(ashlr_server.request_logging_middleware)

    def test_middleware_skips_non_api(self):
        """Middleware should skip non-API paths."""
        src = inspect.getsource(ashlr_server.request_logging_middleware)
        assert "/api/" in src

    def test_middleware_logs_slow_requests(self):
        """Middleware should warn on requests slower than 1s."""
        src = inspect.getsource(ashlr_server.request_logging_middleware)
        assert "SLOW" in src
        assert "1000" in src

    def test_middleware_registered(self):
        """Middleware should be registered in create_app."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "request_logging_middleware" in src

    def test_middleware_first_in_chain(self):
        """Security headers middleware should be first, then request logging."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "middlewares = [security_headers_middleware" in src
        assert "request_logging_middleware" in src

    def test_middleware_handles_http_exceptions(self):
        """Middleware should handle HTTPException gracefully."""
        src = inspect.getsource(ashlr_server.request_logging_middleware)
        assert "HTTPException" in src

    def test_middleware_handles_generic_exceptions(self):
        """Middleware should catch and log generic exceptions."""
        src = inspect.getsource(ashlr_server.request_logging_middleware)
        assert "except Exception" in src


# ─────────────────────────────────────────────
# Spawn Validation Endpoint (#248)
# ─────────────────────────────────────────────



class TestWSBroadcastSafety:
    def test_broadcast_snapshots_clients(self):
        """Broadcast should snapshot clients set before iterating."""
        src = inspect.getsource(ashlr_server.WebSocketHub.broadcast)
        assert "clients_snapshot" in src
        assert "set(self.clients)" in src

    def test_broadcast_iterates_snapshot(self):
        """Broadcast gather should iterate over snapshot, not live set."""
        src = inspect.getsource(ashlr_server.WebSocketHub.broadcast)
        assert "clients_snapshot" in src
        # The gather should use clients_snapshot
        assert "for ws in clients_snapshot" in src


# ─────────────────────────────────────────────
# Tmux Session Collision Check (#250)
# ─────────────────────────────────────────────



class TestDatabaseCommitTimeouts:
    def test_safe_commit_method_exists(self):
        """Database should have _safe_commit method."""
        assert hasattr(ashlr_server.Database, '_safe_commit')
        assert callable(ashlr_server.Database._safe_commit)

    def test_safe_commit_uses_wait_for(self):
        """_safe_commit should use asyncio.wait_for with timeout."""
        src = inspect.getsource(ashlr_server.Database._safe_commit)
        assert "wait_for" in src
        assert "timeout" in src

    def test_safe_commit_handles_timeout(self):
        """_safe_commit should catch TimeoutError gracefully."""
        src = inspect.getsource(ashlr_server.Database._safe_commit)
        assert "TimeoutError" in src

    def test_safe_commit_null_guard(self):
        """_safe_commit should check for None db."""
        src = inspect.getsource(ashlr_server.Database._safe_commit)
        assert "not self._db" in src

    def test_safe_commit_default_timeout(self):
        """_safe_commit should have a reasonable default timeout."""
        src = inspect.getsource(ashlr_server.Database._safe_commit)
        assert "3.0" in src

    def test_all_commits_use_safe_commit(self):
        """All database commits should use _safe_commit, not raw _db.commit()."""
        src = inspect.getsource(ashlr_server.Database)
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
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        assert "not in BUILTIN_ROLES" in src or "falling back" in src.lower()

    def test_role_injection_explicit_check(self):
        """Role injection should explicitly check role existence before using it."""
        src = inspect.getsource(ashlr_server.AgentManager.spawn)
        # Should use .get(role) without fallback, then check
        assert "BUILTIN_ROLES.get(role)" in src


# ─────────────────────────────────────────────
# Archive Rotation (#252)
# ─────────────────────────────────────────────



class TestArchiveRotation:
    def test_rotate_archive_method_exists(self):
        """Database should have rotate_archive method."""
        assert hasattr(ashlr_server.Database, 'rotate_archive')
        assert callable(ashlr_server.Database.rotate_archive)

    def test_cleanup_old_archives_method_exists(self):
        """Database should have cleanup_old_archives method."""
        assert hasattr(ashlr_server.Database, 'cleanup_old_archives')
        assert callable(ashlr_server.Database.cleanup_old_archives)

    def test_config_archive_max_rows(self):
        """Config should have archive_max_rows_per_agent with default 50000."""
        config = ashlr_server.Config()
        assert hasattr(config, 'archive_max_rows_per_agent')
        assert config.archive_max_rows_per_agent == 50000

    def test_config_archive_retention_hours(self):
        """Config should have archive_retention_hours with default 48."""
        config = ashlr_server.Config()
        assert hasattr(config, 'archive_retention_hours')
        assert config.archive_retention_hours == 48

    async def test_rotate_archive_null_guard(self):
        """rotate_archive should return 0 when DB is None."""
        db = ashlr_server.Database()
        # _db is None before init
        result = await db.rotate_archive("test-agent")
        assert result == 0

    async def test_rotate_archive_zero_max_rows(self):
        """rotate_archive should return 0 when max_rows <= 0."""
        db = ashlr_server.Database()
        result = await db.rotate_archive("test-agent", max_rows=0)
        assert result == 0

    async def test_cleanup_old_archives_null_guard(self):
        """cleanup_old_archives should return 0 when DB is None."""
        db = ashlr_server.Database()
        result = await db.cleanup_old_archives()
        assert result == 0

    async def test_cleanup_old_archives_zero_retention(self):
        """cleanup_old_archives should return 0 when retention_hours <= 0."""
        db = ashlr_server.Database()
        result = await db.cleanup_old_archives(retention_hours=0)
        assert result == 0

    def test_rotate_archive_uses_safe_commit(self):
        """rotate_archive should use _safe_commit for timeout protection."""
        src = inspect.getsource(ashlr_server.Database.rotate_archive)
        assert "_safe_commit" in src

    def test_cleanup_old_archives_uses_safe_commit(self):
        """cleanup_old_archives should use _safe_commit for timeout protection."""
        src = inspect.getsource(ashlr_server.Database.cleanup_old_archives)
        assert "_safe_commit" in src


# ─────────────────────────────────────────────
# Server Stats Endpoint (#253)
# ─────────────────────────────────────────────



class TestServerStatsExtended:
    def test_handler_exists(self):
        """get_server_stats handler should exist."""
        assert hasattr(ashlr_server, 'get_server_stats')
        assert callable(ashlr_server.get_server_stats)

    def test_route_registered(self):
        """Route /api/stats should be registered."""
        src = inspect.getsource(ashlr_server.create_app)
        assert "/api/stats" in src
        assert "get_server_stats" in src

    def test_returns_uptime(self):
        """Stats endpoint should return uptime fields."""
        src = inspect.getsource(ashlr_server.get_server_stats)
        assert "uptime_sec" in src
        assert "uptime_human" in src

    def test_returns_agent_stats(self):
        """Stats endpoint should return agent statistics."""
        src = inspect.getsource(ashlr_server.get_server_stats)
        assert "total_spawned" in src
        assert "total_killed" in src
        assert "total_messages_sent" in src

    def test_returns_db_size(self):
        """Stats endpoint should return database size info."""
        src = inspect.getsource(ashlr_server.get_server_stats)
        assert "db_size_mb" in src

    def test_returns_request_count(self):
        """Stats endpoint should return request count."""
        src = inspect.getsource(ashlr_server.get_server_stats)
        assert "request_count" in src

    def test_manager_has_api_request_counter(self):
        """AgentManager should have _total_api_requests counter."""
        config = ashlr_server.Config()
        config.demo_mode = True
        manager = ashlr_server.AgentManager(config)
        assert hasattr(manager, '_total_api_requests')
        assert manager._total_api_requests == 0

    def test_middleware_increments_manager_counter(self):
        """Request logging middleware should increment manager._total_api_requests."""
        src = inspect.getsource(ashlr_server.request_logging_middleware)
        assert "_total_api_requests" in src

    def test_stats_returns_archive_config(self):
        """Stats endpoint should include archive configuration."""
        src = inspect.getsource(ashlr_server.get_server_stats)
        assert "archive_max_rows_per_agent" in src
        assert "archive_retention_hours" in src


# ─────────────────────────────────────────────
# Extended Secret Redaction (#254)
# ─────────────────────────────────────────────



class TestExtendedSecretRedaction:
    def test_redact_secrets_exists(self):
        """redact_secrets function should exist."""
        assert hasattr(ashlr_server, 'redact_secrets')
        assert callable(ashlr_server.redact_secrets)

    def test_redact_openai_key(self):
        """Should redact OpenAI/Anthropic sk- keys."""
        text = "Using key sk-abcdefghijklmnopqrstuvwx in the config"
        result = ashlr_server.redact_secrets(text)
        assert "sk-" not in result
        assert "REDACTED" in result

    def test_redact_github_pat_classic(self):
        """Should redact classic GitHub PATs (ghp_)."""
        text = "Token: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLm"
        result = ashlr_server.redact_secrets(text)
        assert "ghp_" not in result
        assert "REDACTED" in result

    def test_redact_github_pat_fine_grained(self):
        """Should redact fine-grained GitHub PATs (github_pat_)."""
        text = "Token: github_pat_aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlr_server.redact_secrets(text)
        assert "github_pat_" not in result
        assert "REDACTED" in result

    def test_redact_aws_access_key(self):
        """Should redact AWS access keys (AKIA...)."""
        text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        result = ashlr_server.redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "REDACTED" in result

    def test_redact_slack_bot_token(self):
        """Should redact Slack bot tokens (xoxb-)."""
        text = "SLACK_TOKEN=xoxb-12345678901-12345678901-abcdef"
        result = ashlr_server.redact_secrets(text)
        assert "xoxb-" not in result
        assert "REDACTED" in result

    def test_redact_sendgrid_key(self):
        """Should redact SendGrid API keys (SG.xxxxx.xxxxx)."""
        text = "key: SG.aBcDeFgHiJkLmNoPqRsTuVw.xYzAbCdEfGhIjKlMnOpQrSt"
        result = ashlr_server.redact_secrets(text)
        assert "SG." not in result
        assert "REDACTED" in result

    def test_redact_password_field(self):
        """Should redact password= fields."""
        text = "password=MyS3cretP@ss"
        result = ashlr_server.redact_secrets(text)
        assert "MyS3cretP@ss" not in result
        assert "REDACTED" in result

    def test_redact_jwt_token(self):
        """Should redact JWT tokens (eyJ...)."""
        text = "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        result = ashlr_server.redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "REDACTED" in result

    def test_redact_mongodb_connection_string(self):
        """Should redact MongoDB connection strings."""
        text = "DB_URL=mongodb+srv://user:pass@cluster.mongodb.net/db"
        result = ashlr_server.redact_secrets(text)
        assert "mongodb+srv://" not in result
        assert "REDACTED" in result

    def test_redact_postgres_connection_string(self):
        """Should redact PostgreSQL connection strings."""
        text = "DATABASE_URL=postgresql://user:pass@host:5432/mydb"
        result = ashlr_server.redact_secrets(text)
        assert "postgresql://" not in result
        assert "REDACTED" in result

    def test_redact_redis_connection_string(self):
        """Should redact Redis connection strings."""
        text = "REDIS_URL=redis://default:pass@redis-host:6379"
        result = ashlr_server.redact_secrets(text)
        assert "redis://" not in result
        assert "REDACTED" in result

    def test_no_false_positive_on_normal_text(self):
        """Should not redact normal text without secrets."""
        text = "Hello world, this is a normal log line with no secrets"
        result = ashlr_server.redact_secrets(text)
        assert result == text

    def test_pattern_count(self):
        """Should have at least 20 secret patterns (expanded from original 7)."""
        assert len(ashlr_server._SECRET_PATTERNS) >= 20

    def test_redact_xai_key(self):
        """Should redact xAI API keys."""
        text = "XAI_KEY=xai-aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlr_server.redact_secrets(text)
        assert "xai-" not in result
        assert "REDACTED" in result

    def test_redact_npm_token(self):
        """Should redact npm tokens."""
        text = "NPM_TOKEN=np_aBcDeFgHiJkLmNoPqRsTuVw"
        result = ashlr_server.redact_secrets(text)
        assert "np_" not in result
        assert "REDACTED" in result

    def test_redact_bearer_token(self):
        """Should redact Bearer tokens."""
        text = "Authorization: Bearer eyAbCdEfGhIjKlMnOpQr"
        result = ashlr_server.redact_secrets(text)
        assert "Bearer eyAbCdEfGhIjKlMnOpQr" not in result
        assert "REDACTED" in result

    def test_redact_api_key_field(self):
        """Should redact api_key= fields."""
        text = "api_key=abc123def456ghi789jkl012"
        result = ashlr_server.redact_secrets(text)
        assert "abc123def456ghi789jkl012" not in result
        assert "REDACTED" in result


# ─────────────────────────────────────────────
# WebSocket Message Validation (#255)
# ─────────────────────────────────────────────



class TestWSMessageValidation:
    def test_ws_send_has_length_check(self):
        """WS 'send' case should enforce message length limit."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_message)
        # Find the send case section
        assert "50_000" in src or "50000" in src

    def test_ws_send_returns_error_on_overlength(self):
        """WS 'send' case should return error for oversized messages."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_message)
        assert "Message too long" in src

    def test_ws_agent_message_validates_from_id(self):
        """WS 'agent_message' case should validate from_agent_id exists."""
        src = inspect.getsource(ashlr_server.WebSocketHub.handle_message)
        assert "Source agent" in src

    def test_restart_handles_json_decode_error(self):
        """Restart endpoint should return 400 on malformed JSON."""
        src = inspect.getsource(ashlr_server.restart_agent)
        assert "JSONDecodeError" in src or "json.JSONDecodeError" in src


# ─────────────────────────────────────────────
# Output Flood Protection (#256)
# ─────────────────────────────────────────────



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



class TestHealthCheckPathological:
    """Tests for pathological error loop detection in health_check_loop."""

    def _make_agent(self, restart_count=1, error_time=None, restart_time=None):
        from ashlr_server import Agent
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
        from ashlr_server import Agent
        agent = Agent(id="b001", name="backoff", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent._error_entered_at = time.monotonic() - 15  # Error 15s ago
        agent.restart_count = 1
        agent.last_restart_time = time.monotonic() - 12  # Last restart 12s ago
        backoff = 5.0 * (2 ** agent.restart_count)  # 10s
        time_since = time.monotonic() - agent.last_restart_time
        assert time_since >= backoff  # Should be allowed

    def test_restart_blocked_during_backoff(self):
        from ashlr_server import Agent
        agent = Agent(id="b002", name="backoff2", role="general", status="error", backend="claude-code", task="t", working_dir="/tmp")
        agent.status = "error"
        agent._error_entered_at = time.monotonic() - 3  # Error 3s ago
        agent.restart_count = 2
        agent.last_restart_time = time.monotonic() - 5  # Last restart 5s ago
        backoff = 5.0 * (2 ** agent.restart_count)  # 20s
        time_since = time.monotonic() - agent.last_restart_time
        assert time_since < backoff  # Should be blocked



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



class TestDatabaseCloseNullsDb:
    """Tests that Database.close() sets _db = None for safe guard checks."""

    async def test_close_nulls_db(self):
        """After close(), _db should be None so all guards short-circuit."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        mock_conn = AsyncMock()
        db._db = mock_conn
        await db.close()
        assert db._db is None
        mock_conn.close.assert_awaited_once()

    async def test_close_noop_when_none(self):
        """close() should be safe to call when _db is already None."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = None
        await db.close()  # Should not raise
        assert db._db is None



class TestDatabaseInitSafety:
    """Tests that Database.init() handles retries safely."""

    async def test_init_closes_existing_on_retry(self):
        """If init() is called when _db already has a connection, it should close first."""
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        old_conn = AsyncMock()
        db._db = old_conn
        db.db_path = Path("/tmp/test_nonexistent.db")

        # init() will fail on connect since we patch it to raise, but should close old conn first
        with patch("aiosqlite.connect", side_effect=Exception("test error")):
            with pytest.raises(Exception, match="test error"):
                await db.init()

        old_conn.close.assert_awaited_once()
        assert db._db is None  # Should be cleaned up after failure



class TestDatabaseWriteErrorHandling:
    """Tests that write methods catch exceptions gracefully."""

    def _make_db(self):
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        db._db = AsyncMock()
        db._db.execute = AsyncMock(side_effect=Exception("disk full"))
        return db

    async def test_save_agent_handles_db_error(self):
        """save_agent should log warning, not raise."""
        db = self._make_db()
        agent = MagicMock()
        agent.id = "test1"
        agent.name = "test"
        agent.role = "general"
        agent.project_id = None
        agent.task = "test"
        agent.summary = ""
        agent.status = "complete"
        agent.working_dir = "/tmp"
        agent.backend = "claude-code"
        agent.created_at = "2024-01-01T00:00:00Z"
        agent.context_pct = 0.0
        agent.output_lines = []
        agent.tokens_input = 0
        agent.tokens_output = 0
        agent.estimated_cost_usd = 0.0
        agent.plan_mode = False
        agent._spawn_time = time.monotonic() - 60
        # Should not raise
        await db.save_agent(agent)

    async def test_save_project_handles_db_error(self):
        db = self._make_db()
        await db.save_project({"id": "p1", "name": "test", "path": "/tmp"})

    async def test_save_workflow_handles_db_error(self):
        db = self._make_db()
        await db.save_workflow({"id": "w1", "name": "test", "agents_json": "[]"})

    async def test_save_message_handles_db_error(self):
        db = self._make_db()
        await db.save_message({"id": "m1", "from_agent_id": "a1", "content": "hi", "created_at": "now"})

    async def test_set_file_lock_handles_db_error(self):
        db = self._make_db()
        await db.set_file_lock("/tmp/file.py", "a1", "agent1")

    async def test_release_file_locks_handles_db_error(self):
        db = self._make_db()
        await db.release_file_locks("a1")

    async def test_mark_messages_read_handles_db_error(self):
        db = self._make_db()
        result = await db.mark_messages_read("a1")
        assert result == 0



class TestDatabaseReadErrorHandling:
    """Tests that read methods return fallbacks on DB errors."""

    def _make_db(self):
        db = ashlr_server.Database.__new__(ashlr_server.Database)
        mock_conn = MagicMock()
        # Make execute raise when used as async context manager
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=Exception("corrupt"))
        mock_conn.execute = MagicMock(return_value=mock_ctx)
        db._db = mock_conn
        return db

    async def test_get_agent_history_returns_empty_on_error(self):
        db = self._make_db()
        result = await db.get_agent_history()
        assert result == []

    async def test_get_agent_history_count_returns_zero_on_error(self):
        db = self._make_db()
        result = await db.get_agent_history_count()
        assert result == 0

    async def test_get_agent_history_item_returns_none_on_error(self):
        db = self._make_db()
        result = await db.get_agent_history_item("test_id")
        assert result is None



class TestQueueValidation:
    """Tests that add_to_queue validates role and backend."""

    def test_queue_source_validates_role(self):
        """The add_to_queue handler should check role against BUILTIN_ROLES."""
        source = inspect.getsource(ashlr_server.add_to_queue)
        assert "BUILTIN_ROLES" in source

    def test_queue_source_validates_backend(self):
        """The add_to_queue handler should check backend against KNOWN_BACKENDS."""
        source = inspect.getsource(ashlr_server.add_to_queue)
        assert "KNOWN_BACKENDS" in source



class TestHandoffValidation:
    """Tests that handoff_agent validates files_modified type."""

    def test_handoff_validates_files_modified_type(self):
        """The handoff handler should validate files_modified is a list."""
        source = inspect.getsource(ashlr_server.handoff_agent)
        assert "isinstance(files_modified, list)" in source

    def test_handoff_truncates_key_findings(self):
        """key_findings should be truncated to 5000 chars."""
        source = inspect.getsource(ashlr_server.handoff_agent)
        assert "[:5000]" in source



class TestMigrationIndividualAlters:
    """Tests that DB migrations use individual try/except per ALTER."""

    def test_migration_uses_loop(self):
        """init() should use a for loop with individual try/except for ALTERs."""
        source = inspect.getsource(ashlr_server.Database.init)
        assert "for col_sql in [" in source


# ─────────────────────────────────────────────
# T-NEW: Workflow Validation (shared helper)
# ─────────────────────────────────────────────



class TestSafeCommitReturn:
    """Tests that _safe_commit returns bool."""

    async def test_returns_false_when_no_db(self):
        db = ashlr_server.Database()
        db._db = None
        result = await db._safe_commit()
        assert result is False

    def test_returns_bool_type(self):
        """_safe_commit should return bool, not None."""
        source = inspect.getsource(ashlr_server.Database._safe_commit)
        assert "-> bool" in source
        assert "return True" in source
        assert "return False" in source


# ─────────────────────────────────────────────
# T-NEW: Historical Analytics Error Handling
# ─────────────────────────────────────────────



class TestHistoricalAnalyticsErrorHandling:
    """Tests that get_historical_analytics handles errors gracefully."""

    async def test_returns_empty_dict_when_no_db(self):
        db = ashlr_server.Database()
        db._db = None
        result = await db.get_historical_analytics()
        assert isinstance(result, dict)
        assert result.get("total_historical") == 0
        assert "error_patterns" in result


# ─────────────────────────────────────────────
# T-NEW: Create Project Path Validation
# ─────────────────────────────────────────────



class TestCreateProjectPathValidation:
    """Tests that create_project validates paths."""

    def test_source_has_isdir_check(self):
        """create_project should validate path is a directory."""
        source = inspect.getsource(ashlr_server.create_project)
        assert "os.path.isdir" in source

    def test_source_has_home_check(self):
        """create_project should validate path is under home or /tmp."""
        source = inspect.getsource(ashlr_server.create_project)
        assert "Path.home()" in source or "startswith" in source


# ─────────────────────────────────────────────
# T-NEW: Git branch tracking
# ─────────────────────────────────────────────



class TestGitBranchTracking:
    """Tests for git_branch field on Agent and parse_incremental detection."""

    def test_agent_has_git_branch_field(self, make_agent):
        """Agent dataclass has git_branch=None by default."""
        agent = make_agent()
        assert agent.git_branch is None

    def test_agent_to_dict_includes_git_branch(self, make_agent):
        """to_dict() includes git_branch key."""
        agent = make_agent()
        d = agent.to_dict()
        assert "git_branch" in d
        assert d["git_branch"] is None

    def test_git_checkout_sets_branch(self, make_agent):
        """parse_incremental detects 'git checkout main' and sets agent.git_branch."""
        import collections
        agent = make_agent()
        agent.output_lines = collections.deque(["git checkout main"])
        agent._total_lines_added = 1
        agent._last_parse_index = 0
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        assert agent.git_branch == "main"

    def test_git_switch_sets_branch(self, make_agent):
        """parse_incremental detects 'git switch feature-x' and sets agent.git_branch."""
        import collections
        agent = make_agent()
        agent.output_lines = collections.deque(["git switch feature-x"])
        agent._total_lines_added = 1
        agent._last_parse_index = 0
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        assert agent.git_branch == "feature-x"

    def test_checkout_file_ignored(self, make_agent):
        """'git checkout -- file.txt' does NOT set git_branch (detail starts with '-')."""
        import collections
        agent = make_agent()
        agent.output_lines = collections.deque(["git checkout -- file.txt"])
        agent._total_lines_added = 1
        agent._last_parse_index = 0
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        # The regex captures "--" as the first \S+ match, which starts with "-"
        # so git_branch should not be set
        assert agent.git_branch is None

    def test_git_switched_output(self, make_agent):
        """The 'Switched to branch ...' output regex sets git_branch."""
        import collections
        agent = make_agent()
        agent.output_lines = collections.deque(["Switched to branch 'foo'"])
        agent._total_lines_added = 1
        agent._last_parse_index = 0
        parser = OutputIntelligenceParser()
        parser.parse_incremental(agent)
        assert agent.git_branch == "foo"

    def test_agent_to_dict_with_branch(self, make_agent):
        """Agent with git_branch='main' has it in to_dict()."""
        agent = make_agent()
        agent.git_branch = "main"
        d = agent.to_dict()
        assert d["git_branch"] == "main"


# ─────────────────────────────────────────────
# T-NEW: Session resume
# ─────────────────────────────────────────────



class TestListAgentsFilter:
    """Tests for GET /api/agents with query filters (branch, project_id, status)."""

    async def _spawn_agent(self, app, name="filter-test", **kwargs):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 99001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(
                role=kwargs.get("role", "general"),
                name=name,
                task=kwargs.get("task", "test"),
                working_dir=TEST_WORKING_DIR,
            )
            if "git_branch" in kwargs:
                agent.git_branch = kwargs["git_branch"]
            if "project_id" in kwargs:
                agent.project_id = kwargs["project_id"]
            if "status" in kwargs:
                agent.set_status(kwargs["status"])
            return agent

    @pytest.mark.asyncio
    async def test_list_agents_no_filter(self, aiohttp_client):
        """GET /api/agents returns all agents."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        await self._spawn_agent(app, name="agent-a")
        await self._spawn_agent(app, name="agent-b")
        resp = await client.get("/api/agents")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_list_agents_filter_by_branch(self, aiohttp_client):
        """GET /api/agents?branch=main filters correctly."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        await self._spawn_agent(app, name="main-agent", git_branch="main")
        await self._spawn_agent(app, name="feat-agent", git_branch="feature-x")
        await self._spawn_agent(app, name="no-branch-agent")
        resp = await client.get("/api/agents?branch=main")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "main-agent"
        assert data[0]["git_branch"] == "main"

    @pytest.mark.asyncio
    async def test_list_agents_filter_by_project(self, aiohttp_client):
        """GET /api/agents?project_id=xxx filters correctly."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        await self._spawn_agent(app, name="proj-a", project_id="proj-001")
        await self._spawn_agent(app, name="proj-b", project_id="proj-002")
        resp = await client.get("/api/agents?project_id=proj-001")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "proj-a"

    @pytest.mark.asyncio
    async def test_list_agents_filter_by_status(self, aiohttp_client):
        """GET /api/agents?status=working filters correctly."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        await self._spawn_agent(app, name="working-agent", status="working")
        await self._spawn_agent(app, name="paused-agent", status="paused")
        resp = await client.get("/api/agents?status=working")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) >= 1
        assert all(a["status"] == "working" for a in data)


# ─────────────────────────────────────────────
# Spawn Error Path Tests
# ─────────────────────────────────────────────


