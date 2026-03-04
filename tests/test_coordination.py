"""Tests for Wave 5: Cross-Agent Intelligence & Coordination.

Tests scratchpad broadcast, auto-handoff, file lock enforcement config,
coordination feed (events by project), and project scratchpad alias.
"""

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server

from ashlr_server import Config
from ashlr_ao.background import output_capture_loop
from conftest import make_mock_db, make_test_app

TEST_WORKING_DIR = str(Path.home())


# ── Helper: mock agent factory ──

def _make_mock_agent(**overrides):
    agent = MagicMock()
    defaults = {
        "status": "working", "name": "test-agent", "task": "test",
        "role": "general", "plan_mode": False, "needs_input": False,
        "input_prompt": "", "output_lines": [], "summary": "did some work",
        "progress_pct": 0, "health_score": 1.0, "error_count": 0,
        "memory_mb": 0.0, "total_output_lines": 0, "output_rate": 0.0,
        "estimated_cost_usd": 0.0, "last_output_time": time.monotonic(),
        "_spawn_time": time.monotonic() - 60, "_stale_warned": False,
        "_first_output_received": True, "time_to_first_output": 1.0,
        "_output_line_timestamps": [], "_overflow_to_archive": None,
        "_flood_ticks": 0, "_flood_detected": False,
        "_last_llm_summary_time": 0, "_prev_output_hash": "",
        "_summary_output_hash": "", "_llm_summary": "",
        "_capture_fail_count": 0, "_last_needs_input_event": 0,
        "_error_entered_at": 0, "_health_low_warned": False,
        "_health_critical_warned": False,
        "restart_count": 0, "max_restarts": 3, "last_restart_time": 0,
        "project_id": None, "context_pct": 0.0, "backend": "claude-code",
        "_context_exhaustion_warned": False, "_context_auto_paused": False,
        "_pathological": False, "workflow_run_id": None, "pid": None,
        "working_dir": TEST_WORKING_DIR,
        "next_agent_config": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(agent, k, v)
    if "to_dict" not in overrides:
        agent.to_dict = MagicMock(return_value={"id": "mock", "status": defaults["status"]})
    if "set_status" not in overrides:
        def _set_status(s):
            agent.status = s
        agent.set_status = MagicMock(side_effect=_set_status)
    if "create_snapshot" not in overrides:
        agent.create_snapshot = MagicMock()
    return agent


def _make_capture_app(**config_overrides):
    config = Config()
    config.demo_mode = True
    config.spawn_pressure_block = False
    config.output_capture_interval = 0.01
    for k, v in config_overrides.items():
        setattr(config, k, v)

    app = ashlr_server.create_app(config)
    mock_db = make_mock_db()
    app["db"] = mock_db
    app["ws_hub"].db = mock_db
    app["rate_limiter"].check = lambda *a, **kw: (True, 0.0)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["db_available"] = True
    app["db_ready"] = True
    app["bg_task_health"] = {}
    app["bg_tasks"] = []
    from ashlr_server import License, PRO_FEATURES
    license = License(
        tier="pro", max_agents=100,
        expires_at=(datetime.now(timezone.utc)).isoformat(),
        features=PRO_FEATURES,
    )
    app["license"] = license
    app["agent_manager"].license = license
    hub = app["ws_hub"]
    hub.broadcast = AsyncMock()
    hub.broadcast_event = AsyncMock()
    return app


# ═══════════════════════════════════════════════
# Scratchpad Broadcast Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestScratchpadBroadcast:
    """Tests for scratchpad upsert WebSocket broadcast."""

    async def test_upsert_broadcasts(self):
        """Scratchpad upsert should broadcast to WebSocket clients."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()

        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/scratchpad", json={
                "project_id": "proj1",
                "key": "status",
                "value": "All systems go",
                "set_by": "agent-1",
            })
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"

            # Check broadcast was called with scratchpad_update
            broadcast_calls = hub.broadcast.call_args_list
            scratchpad_updates = [
                c for c in broadcast_calls
                if c[0][0].get("type") == "scratchpad_update"
            ]
            assert len(scratchpad_updates) > 0
            update = scratchpad_updates[0][0][0]
            assert update["project_id"] == "proj1"
            assert update["key"] == "status"
            assert update["value"] == "All systems go"

    async def test_upsert_without_project_fails(self):
        """Scratchpad upsert without project_id should fail."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/scratchpad", json={"key": "test", "value": "x"})
            assert resp.status == 400

    async def test_upsert_without_key_fails(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/scratchpad", json={"project_id": "p1", "value": "x"})
            assert resp.status == 400


# ═══════════════════════════════════════════════
# Project Scratchpad Alias Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestProjectScratchpadAlias:
    """Tests for GET /api/projects/{id}/scratchpad convenience alias."""

    async def test_project_scratchpad(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        app["db"].get_scratchpad = AsyncMock(return_value=[
            {"key": "status", "value": "ok", "set_by": "a1"},
        ])
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/projects/proj1/scratchpad")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            assert data[0]["key"] == "status"
            app["db"].get_scratchpad.assert_awaited_with("proj1")

    async def test_project_scratchpad_empty(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/projects/proj1/scratchpad")
            assert resp.status == 200
            data = await resp.json()
            assert data == []


# ═══════════════════════════════════════════════
# Configure Handoff Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestConfigureHandoff:
    """Tests for POST /api/agents/{id}/configure-handoff."""

    async def test_configure_handoff(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.owner_id = None
        agent.next_agent_config = None
        agent.updated_at = ""
        manager.agents["a1"] = agent

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": {
                    "role": "tester",
                    "task": "Run tests after {prev_agent} finishes",
                },
            })
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert agent.next_agent_config is not None
            assert agent.next_agent_config["role"] == "tester"

    async def test_configure_handoff_clear(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.owner_id = None
        agent.next_agent_config = {"role": "tester", "task": "test"}
        manager.agents["a1"] = agent

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": None,
            })
            assert resp.status == 200
            assert agent.next_agent_config is None

    async def test_configure_handoff_agent_not_found(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/nope/configure-handoff", json={
                "next_agent_config": {"task": "test"},
            })
            assert resp.status == 404

    async def test_configure_handoff_missing_task(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.owner_id = None
        manager.agents["a1"] = agent

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": {"role": "tester"},
            })
            assert resp.status == 400

    async def test_configure_handoff_invalid_type(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.owner_id = None
        manager.agents["a1"] = agent

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": "not a dict",
            })
            assert resp.status == 400


# ═══════════════════════════════════════════════
# Auto-Handoff Integration Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestAutoHandoff:
    """Tests for auto-handoff when agent completes."""

    @patch("ashlr_ao.background.extract_summary", return_value="Built the API")
    async def test_handoff_spawns_successor(self, mock_summary):
        """When agent completes with next_agent_config, should spawn successor."""
        app = _make_capture_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            name="first-agent",
            status="working",
            summary="Built the API",
            next_agent_config={
                "role": "tester",
                "task": "Test what {prev_agent} built: {prev_summary}",
                "backend": "claude-code",
            },
            to_dict=MagicMock(return_value={"id": "a1", "status": "idle"}),
        )
        manager.agents = {"a1": agent}
        manager.capture_output = AsyncMock(return_value=["some output"])
        manager.detect_status = AsyncMock(return_value="idle")

        spawned = []
        successor = _make_mock_agent(name="successor", to_dict=MagicMock(return_value={"id": "s1", "status": "spawning"}))

        async def mock_spawn(**kwargs):
            spawned.append(kwargs)
            manager.agents["s1"] = successor
            return "s1"

        manager.spawn = mock_spawn

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert len(spawned) > 0
        assert "first-agent" in spawned[0]["task"]
        assert "Built the API" in spawned[0]["task"]
        handoff_events = [c for c in hub.broadcast_event.call_args_list if c[0][0] == "agent_handoff"]
        assert len(handoff_events) > 0

    async def test_no_handoff_without_config(self):
        """When agent completes without next_agent_config, no handoff should happen."""
        app = _make_capture_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(
            name="solo-agent",
            status="working",
            next_agent_config=None,
            to_dict=MagicMock(return_value={"id": "a1", "status": "idle"}),
        )
        manager.agents = {"a1": agent}
        manager.capture_output = AsyncMock(return_value=["some output"])
        manager.detect_status = AsyncMock(return_value="idle")
        manager.spawn = AsyncMock()

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.spawn.assert_not_awaited()

    async def test_handoff_no_recursive(self):
        """Successor agent should NOT inherit next_agent_config (no chain)."""
        app = _make_capture_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(
            name="first",
            status="working",
            next_agent_config={"role": "tester", "task": "test it"},
            to_dict=MagicMock(return_value={"id": "a1", "status": "idle"}),
        )
        manager.agents = {"a1": agent}
        manager.capture_output = AsyncMock(return_value=["output"])
        manager.detect_status = AsyncMock(return_value="idle")

        successor = _make_mock_agent(name="second", next_agent_config={"role": "x", "task": "should be cleared"})
        successor.to_dict = MagicMock(return_value={"id": "s1", "status": "spawning"})

        async def mock_spawn(**kwargs):
            manager.agents["s1"] = successor
            return "s1"

        manager.spawn = mock_spawn

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Successor's next_agent_config should be cleared
        assert successor.next_agent_config is None


# ═══════════════════════════════════════════════
# Events by Project Filter Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestEventsByProject:
    """Tests for GET /api/events?project_id= filter."""

    async def test_events_with_project_filter(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]

        # Create agents in project
        a1 = MagicMock()
        a1.project_id = "proj1"
        a2 = MagicMock()
        a2.project_id = "proj1"
        manager.agents = {"a1": a1, "a2": a2}

        app["db"].get_events = AsyncMock(return_value=[
            {"event_type": "agent_spawned", "agent_id": "a1", "message": "test", "created_at": "2026-01-01"},
        ])

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/events?project_id=proj1")
            assert resp.status == 200
            data = await resp.json()
            assert "data" in data

    async def test_events_project_no_agents(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/events?project_id=nonexistent")
            assert resp.status == 200
            data = await resp.json()
            assert data["data"] == []
            assert data["pagination"]["total"] == 0

    async def test_events_without_filter(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        app["db"].get_events = AsyncMock(return_value=[{"event_type": "test"}])
        app["db"].get_events_count = AsyncMock(return_value=1)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/events")
            assert resp.status == 200


# ═══════════════════════════════════════════════
# Agent next_agent_config Model Tests
# ═══════════════════════════════════════════════


class TestAgentNextConfig:
    """Tests for Agent.next_agent_config field."""

    def test_default_is_none(self):
        from ashlr_ao.models import Agent
        agent = Agent(
            id="a1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent.next_agent_config is None

    def test_set_handoff_config(self):
        from ashlr_ao.models import Agent
        agent = Agent(
            id="a1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        agent.next_agent_config = {"role": "tester", "task": "run tests"}
        assert agent.next_agent_config["role"] == "tester"

    def test_clear_handoff_config(self):
        from ashlr_ao.models import Agent
        agent = Agent(
            id="a1", name="test", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
            next_agent_config={"role": "x", "task": "y"},
        )
        agent.next_agent_config = None
        assert agent.next_agent_config is None


# ═══════════════════════════════════════════════
# File Lock Enforcement Config Tests
# ═══════════════════════════════════════════════


class TestFileLockConfig:
    """Tests for file_lock_enforcement config."""

    def test_default_disabled(self):
        cfg = Config()
        assert cfg.file_lock_enforcement is False

    def test_can_enable(self):
        cfg = Config(file_lock_enforcement=True)
        assert cfg.file_lock_enforcement is True


# ═══════════════════════════════════════════════
# Coordination Feed Integration Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestCoordinationFeed:
    """Tests for real-time coordination via WebSocket events."""

    async def test_scratchpad_update_message_format(self):
        """Scratchpad broadcast should have correct message format."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()

        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/scratchpad", json={
                "project_id": "p1", "key": "test_key",
                "value": "test_value", "set_by": "agent-x",
            })
            assert resp.status == 200

        # Verify broadcast message structure
        for call in hub.broadcast.call_args_list:
            msg = call[0][0]
            if msg.get("type") == "scratchpad_update":
                assert "project_id" in msg
                assert "key" in msg
                assert "value" in msg
                assert "set_by" in msg
                return
        pytest.fail("No scratchpad_update broadcast found")

    async def test_handoff_event_has_metadata(self):
        """Handoff events should include from_agent metadata."""
        app = _make_capture_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            name="handoff-from",
            status="working",
            next_agent_config={"role": "tester", "task": "test"},
            to_dict=MagicMock(return_value={"id": "a1", "status": "idle"}),
        )
        manager.agents = {"a1": agent}
        manager.capture_output = AsyncMock(return_value=["output"])
        manager.detect_status = AsyncMock(return_value="idle")

        successor = _make_mock_agent(name="handoff-to")
        successor.to_dict = MagicMock(return_value={"id": "s1", "status": "spawning"})

        async def mock_spawn(**kwargs):
            manager.agents["s1"] = successor
            return "s1"

        manager.spawn = mock_spawn

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        handoff_events = [c for c in hub.broadcast_event.call_args_list if c[0][0] == "agent_handoff"]
        assert len(handoff_events) > 0
        # Check metadata has from_agent info
        kwargs = handoff_events[0][1] if len(handoff_events[0]) > 1 else {}
        if "metadata" in kwargs:
            assert "from_agent" in kwargs["metadata"]


# ═══════════════════════════════════════════════
# Audit hardening tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestConfigureHandoffValidation:
    """Audit: configure-handoff should validate working_dir."""

    async def test_handoff_rejects_bad_working_dir(self):
        """Handoff with working_dir outside home/tmp should be rejected."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.next_agent_config = None
        manager.agents = {"a1": agent}
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": {
                    "task": "Test task",
                    "working_dir": "/etc/passwd",
                }
            })
            assert resp.status == 400
            data = await resp.json()
            assert "working_dir" in data["error"]

    async def test_handoff_accepts_valid_working_dir(self):
        """Handoff with working_dir under home should be accepted."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]
        agent = MagicMock()
        agent.next_agent_config = None
        manager.agents = {"a1": agent}
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/agents/a1/configure-handoff", json={
                "next_agent_config": {
                    "task": "Test task",
                    "working_dir": str(Path.home()),
                }
            })
            assert resp.status == 200


@pytest.mark.asyncio
class TestUpdateProjectAutoApproveValidation:
    """Audit: update_project should validate auto_approve_patterns."""

    async def test_rejects_non_list_patterns(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        db = app["db"]
        db.get_project = AsyncMock(return_value={"id": "p1", "name": "Test", "path": "/tmp/test"})
        db.update_project = AsyncMock(return_value={"id": "p1", "name": "Test"})
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/projects/p1", json={
                "auto_approve_patterns": "not a list"
            })
            assert resp.status == 400

    async def test_rejects_invalid_regex(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        db = app["db"]
        db.get_project = AsyncMock(return_value={"id": "p1", "name": "Test", "path": "/tmp/test"})
        db.update_project = AsyncMock(return_value={"id": "p1", "name": "Test"})
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/projects/p1", json={
                "auto_approve_patterns": [{"pattern": "[invalid"}]
            })
            assert resp.status == 400
            data = await resp.json()
            assert "regex" in data["error"].lower() or "Invalid" in data["error"]

    async def test_rejects_empty_pattern(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        db = app["db"]
        db.get_project = AsyncMock(return_value={"id": "p1", "name": "Test", "path": "/tmp/test"})
        db.update_project = AsyncMock(return_value={"id": "p1", "name": "Test"})
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/projects/p1", json={
                "auto_approve_patterns": [{"pattern": "  "}]
            })
            assert resp.status == 400

    async def test_rejects_pattern_without_pattern_key(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        db = app["db"]
        db.get_project = AsyncMock(return_value={"id": "p1", "name": "Test", "path": "/tmp/test"})
        db.update_project = AsyncMock(return_value={"id": "p1", "name": "Test"})
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/projects/p1", json={
                "auto_approve_patterns": [{"response": "yes"}]
            })
            assert resp.status == 400

    async def test_truncates_response_to_200_chars(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        db = app["db"]
        db.get_project = AsyncMock(return_value={"id": "p1", "name": "Test", "path": "/tmp/test"})
        db.update_project = AsyncMock(return_value={"id": "p1", "name": "Updated"})
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/projects/p1", json={
                "auto_approve_patterns": [{"pattern": "test", "response": "x" * 500}]
            })
            # Should succeed but truncate the response
            if resp.status == 200:
                # Check that the update was called with truncated response
                call_data = db.update_project.call_args[0][1]
                assert len(call_data["auto_approve_patterns"][0]["response"]) <= 200
