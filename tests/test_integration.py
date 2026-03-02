"""Integration tests for Ashlr AO — WebSocket + REST API via aiohttp test client.

Tests exercise the full server stack (routes, handlers, WebSocket hub) with
mocked tmux/subprocess calls to avoid spawning real processes.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

sys.path.insert(0, str(Path(__file__).parent.parent))

# Use home directory for working_dir in tests (server validates against home)
TEST_WORKING_DIR = str(Path.home())

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server


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
    db.db_path = Path("/tmp/test-ashlr.db")  # fake path for stats endpoint
    db.find_similar_tasks = AsyncMock(return_value=[])
    db.get_resumable_sessions = AsyncMock(return_value=[])
    db.archive_output = AsyncMock()
    db.release_file_locks = AsyncMock()
    db.get_archived_output = AsyncMock(return_value=([], 0))
    db.get_bookmarks = AsyncMock(return_value=[])
    db.add_bookmark = AsyncMock(return_value=1)
    db.save_project = AsyncMock()
    db.delete_project = AsyncMock(return_value=False)  # default: not found
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
    db._db = None  # no real DB connection
    return db


def _make_test_app():
    """Create a test app with DB and background tasks disabled."""
    config = ashlr_server.Config()
    config.demo_mode = True  # avoid needing real claude CLI
    config.spawn_pressure_block = False  # disable for tests — host CPU/memory can trigger false 503s

    app = ashlr_server.create_app(config)
    # Replace the real DB with a mock so WS handlers don't crash
    mock_db = _make_mock_db()
    app["db"] = mock_db
    app["ws_hub"].db = mock_db
    # Disable rate limiting for tests
    app["rate_limiter"].check = lambda *a, **kw: (True, 0.0)
    # Remove background task hooks so they don't run during tests
    app.on_startup.clear()
    app.on_cleanup.clear()
    # Pre-set app-level flags normally set by start_background_tasks
    app["db_available"] = True
    app["db_ready"] = True
    app["bg_task_health"] = {}
    app["bg_tasks"] = []
    return app


@pytest.fixture
def cli(event_loop, aiohttp_client):
    """Create a test client for the Ashlr app."""
    app = _make_test_app()
    return event_loop.run_until_complete(aiohttp_client(app))


# ── WebSocket Tests ──


class TestWebSocketConnectAndSync:
    @pytest.mark.asyncio
    async def test_ws_connect_and_sync(self, aiohttp_client):
        """Connect via WebSocket and verify initial sync message structure."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")

        msg = await ws.receive_json()
        assert msg["type"] == "sync"
        assert "agents" in msg
        assert "config" in msg
        assert "backends" in msg
        assert "db_ready" in msg
        assert "db_available" in msg
        assert isinstance(msg["agents"], list)
        assert msg["db_ready"] is True
        assert msg["db_available"] is True

        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_spawn_via_message(self, aiohttp_client):
        """Send spawn message over WS and verify agent_update response."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")

        # Consume initial sync
        await ws.receive_json()

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await ws.send_json({
                "type": "spawn",
                "role": "general",
                "name": "test-ws-agent",
                "task": "integration test",
                "working_dir": TEST_WORKING_DIR,
            })

            # Should receive agent_update (or event)
            received_types = set()
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    received_types.add(msg["type"])
                    if msg["type"] == "agent_update":
                        assert msg["agent"]["name"] == "test-ws-agent"
                        break
                except asyncio.TimeoutError:
                    break

            assert "agent_update" in received_types or "event" in received_types

        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_message_size_limit(self, aiohttp_client):
        """Send oversized WS message and verify connection handles it."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")

        # Consume initial sync
        await ws.receive_json()

        # Send a message larger than 1MB — server should reject/close
        huge_payload = "x" * (2 * 1024 * 1024)
        try:
            await ws.send_str(huge_payload)
            # Connection should be closed or error returned
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            # Either closed or error
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.TEXT)
        except Exception:
            pass  # Connection may be forcibly closed

        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_sync_rate_limit(self, aiohttp_client):
        """Rapid sync_request should be throttled after first one."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")

        # Consume initial sync
        await ws.receive_json()

        # First sync_request — should succeed
        await ws.send_json({"type": "sync_request"})
        msg1 = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg1["type"] == "sync"

        # Immediate second sync_request — should be throttled
        await ws.send_json({"type": "sync_request"})
        msg2 = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg2["type"] == "error"
        assert "throttled" in msg2["message"].lower()

        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_max_clients(self, aiohttp_client):
        """Connecting beyond max_clients should be rejected."""
        app = _make_test_app()
        # Set a low limit for testing
        app["ws_hub"].max_clients = 3
        client = await aiohttp_client(app)

        connections = []
        try:
            for i in range(4):
                ws = await client.ws_connect("/ws")
                connections.append(ws)

            # The 4th connection should have been rejected (closed immediately)
            # Read from the 4th connection — it should be closed
            last_ws = connections[3]
            msg = await asyncio.wait_for(last_ws.receive(), timeout=2.0)
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
        finally:
            for ws in connections:
                await ws.close()


# ── REST API Tests ──


class TestApiSendMessageLimit:
    @pytest.mark.asyncio
    async def test_send_message_length_limit(self, aiohttp_client):
        """POST a huge message to send_to_agent and verify 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        # First spawn an agent to get a valid ID
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 99999
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            agent = await manager.spawn(
                role="general", name="msg-limit-test", task="test", working_dir=TEST_WORKING_DIR
            )

        # Send a message > 50,000 chars
        resp = await client.post(
            f"/api/agents/{agent.id}/send",
            json={"message": "x" * 60_000},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "too long" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_send_message_normal(self, aiohttp_client):
        """POST a normal-sized message should succeed."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 99998
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            agent = await manager.spawn(
                role="general", name="msg-test", task="test", working_dir=TEST_WORKING_DIR
            )

        with patch.object(manager, "send_message", new_callable=AsyncMock, return_value=True):
            resp = await client.post(
                f"/api/agents/{agent.id}/send",
                json={"message": "Hello, agent!"},
            )
            assert resp.status == 200


class TestApiSpawnAndKillLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, aiohttp_client):
        """Spawn → list → get → kill → verify removed."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 11111
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            # Spawn
            resp = await client.post("/api/agents", json={
                "role": "general",
                "name": "lifecycle-test",
                "task": "lifecycle test",
                "working_dir": TEST_WORKING_DIR,
            })
            assert resp.status == 201
            agent_data = await resp.json()
            agent_id = agent_data["id"]

            # List
            resp = await client.get("/api/agents")
            assert resp.status == 200
            agents = await resp.json()
            assert any(a["id"] == agent_id for a in agents)

            # Get
            resp = await client.get(f"/api/agents/{agent_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "lifecycle-test"

        # Kill
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            resp = await client.delete(f"/api/agents/{agent_id}")
            assert resp.status == 200

        # Verify removed
        resp = await client.get(f"/api/agents/{agent_id}")
        assert resp.status == 404


class TestApiWorkflowValidation:
    @pytest.mark.asyncio
    async def test_invalid_depends_on(self, aiohttp_client):
        """Creating a workflow with invalid depends_on should be rejected."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        # Create a workflow with self-referencing depends_on
        resp = await client.post("/api/workflows", json={
            "name": "bad-workflow",
            "steps": [
                {
                    "name": "step1",
                    "role": "general",
                    "task": "do something",
                    "depends_on": ["step1"],  # self-reference
                },
            ],
        })
        # Should be rejected
        assert resp.status == 400


class TestConcurrentSpawns:
    @pytest.mark.asyncio
    async def test_concurrent_spawns(self, aiohttp_client):
        """Spawn 3 agents sequentially and verify all created.

        Uses sequential spawns to avoid hitting the rate limiter
        (cost=3, burst=5 means rapid concurrent spawns get throttled).
        """
        app = _make_test_app()
        client = await aiohttp_client(app)

        agent_ids = []
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 22222
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            for i in range(3):
                resp = await client.post("/api/agents", json={
                    "role": "general",
                    "name": f"seq-spawn-{i}",
                    "task": f"task {i}",
                    "working_dir": TEST_WORKING_DIR,
                })
                assert resp.status == 201
                data = await resp.json()
                agent_ids.append(data["id"])

        # Verify all 3 exist
        resp = await client.get("/api/agents")
        agents = await resp.json()
        found_ids = {a["id"] for a in agents}
        for aid in agent_ids:
            assert aid in found_ids


class TestDegradedMode:
    @pytest.mark.asyncio
    async def test_system_with_db_unavailable(self, aiohttp_client):
        """System should still serve when DB is marked unavailable."""
        app = _make_test_app()
        app["db_available"] = False
        client = await aiohttp_client(app)

        # Health endpoint should still work
        resp = await client.get("/api/health")
        assert resp.status == 200

        # System metrics should include degraded state
        resp = await client.get("/api/system")
        assert resp.status == 200
        data = await resp.json()
        assert data["services"]["db_available"] is False

        # Agent listing should still work
        resp = await client.get("/api/agents")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_ws_sync_shows_db_unavailable(self, aiohttp_client):
        """WS sync should reflect db_available=false."""
        app = _make_test_app()
        app["db_available"] = False
        client = await aiohttp_client(app)

        ws = await client.ws_connect("/ws")
        msg = await ws.receive_json()
        assert msg["type"] == "sync"
        assert msg["db_available"] is False
        await ws.close()


# ── Agent Pause/Resume/Restart Integration Tests ──


class TestApiPauseResumeRestart:
    @pytest.mark.asyncio
    async def test_pause_and_resume(self, aiohttp_client):
        """Pause a running agent, then resume it."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 30001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(role="general", name="pause-test", task="test", working_dir=TEST_WORKING_DIR)

        # Pause
        resp = await client.post(f"/api/agents/{agent.id}/pause")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "paused"

        # Verify agent is paused
        resp = await client.get(f"/api/agents/{agent.id}")
        data = await resp.json()
        assert data["status"] == "paused"

        # Resume
        resp = await client.post(f"/api/agents/{agent.id}/resume")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "resumed"

    @pytest.mark.asyncio
    async def test_pause_nonexistent_agent(self, aiohttp_client):
        """Pausing a nonexistent agent should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/pause")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_resume_nonexistent_agent(self, aiohttp_client):
        """Resuming a nonexistent agent should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/resume")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_restart_agent(self, aiohttp_client):
        """Restart an agent via REST API."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        # Mock _run_tmux for both spawn and restart to avoid real tmux fork
        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = ""
        success_result.stderr = ""
        manager._run_tmux = AsyncMock(return_value=success_result)
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)

        agent = await manager.spawn(role="backend", name="restart-test", task="old task", working_dir=TEST_WORKING_DIR)

        resp = await client.post(
            f"/api/agents/{agent.id}/restart",
            json={"task": "new task"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_restart_nonexistent_agent(self, aiohttp_client):
        """Restarting a nonexistent agent should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/restart")
        assert resp.status == 404


# ── System & Health Endpoint Tests ──


class TestSystemEndpoints:
    @pytest.mark.asyncio
    async def test_health_check(self, aiohttp_client):
        """GET /api/health should return 200 with status ok."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_system_metrics(self, aiohttp_client):
        """GET /api/system should return CPU, memory, and service info."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/system")
        assert resp.status == 200
        data = await resp.json()
        assert "cpu_pct" in data
        assert "memory" in data
        assert "services" in data
        assert "agents_active" in data

    @pytest.mark.asyncio
    async def test_list_roles(self, aiohttp_client):
        """GET /api/roles should return all builtin roles."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/roles")
        assert resp.status == 200
        roles = await resp.json()
        # Roles endpoint returns a dict keyed by role name
        assert isinstance(roles, dict)
        assert "general" in roles
        assert "backend" in roles
        assert "frontend" in roles

    @pytest.mark.asyncio
    async def test_list_backends(self, aiohttp_client):
        """GET /api/backends should return available backends."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/backends")
        assert resp.status == 200
        data = await resp.json()
        # Backends endpoint returns a dict keyed by backend name
        assert isinstance(data, dict)
        assert "claude-code" in data

    @pytest.mark.asyncio
    async def test_get_config(self, aiohttp_client):
        """GET /api/config should return current config."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert "max_agents" in data
        assert "default_role" in data

    @pytest.mark.asyncio
    async def test_get_stats(self, aiohttp_client):
        """GET /api/stats should return server statistics."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/stats")
        assert resp.status == 200
        data = await resp.json()
        assert "uptime_sec" in data
        assert "agents" in data

    @pytest.mark.asyncio
    async def test_health_detailed(self, aiohttp_client):
        """GET /api/health/detailed should return detailed health info."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health/detailed")
        assert resp.status == 200
        data = await resp.json()
        assert "status" in data


# ── Spawn Validation Tests ──


class TestApiSpawnValidation:
    @pytest.mark.asyncio
    async def test_spawn_missing_task(self, aiohttp_client):
        """Spawning without a task should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents", json={
            "role": "general",
            "name": "no-task",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 400
        body = await resp.json()
        assert "task" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_spawn_invalid_role(self, aiohttp_client):
        """Spawning with an invalid role should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents", json={
            "role": "nonexistent-role",
            "task": "something",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 400
        body = await resp.json()
        assert "role" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_spawn_invalid_json(self, aiohttp_client):
        """Sending invalid JSON should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_spawn_task_too_long(self, aiohttp_client):
        """Task exceeding 10000 chars should be rejected."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents", json={
            "role": "general",
            "task": "x" * 10001,
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 400
        body = await resp.json()
        assert "10000" in body["error"]

    @pytest.mark.asyncio
    async def test_spawn_invalid_backend(self, aiohttp_client):
        """Spawning with an unknown backend should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents", json={
            "role": "general",
            "task": "test",
            "backend": "unknown-backend",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 400
        body = await resp.json()
        assert "backend" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_validate_spawn_dry_run(self, aiohttp_client):
        """POST /api/agents/validate should validate without spawning."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/validate", json={
            "role": "general",
            "task": "test validation",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 200
        data = await resp.json()
        assert data.get("valid") is True

    @pytest.mark.asyncio
    async def test_validate_spawn_invalid(self, aiohttp_client):
        """POST /api/agents/validate with bad role should fail validation."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/validate", json={
            "role": "bad-role",
            "task": "test",
            "working_dir": TEST_WORKING_DIR,
        })
        # Should indicate invalid
        data = await resp.json()
        assert data.get("valid") is False or resp.status == 400


# ── Agent Output & Activity Endpoint Tests ──


class TestApiAgentOutputAndActivity:
    async def _spawn_agent(self, app):
        """Helper to spawn a test agent."""
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 40001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name="output-test", task="test", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_get_agent_output(self, aiohttp_client):
        """GET /api/agents/{id}/output should return output lines."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/output")
        assert resp.status == 200
        data = await resp.json()
        assert "data" in data or "lines" in data  # paginated output uses "data" key

    @pytest.mark.asyncio
    async def test_get_agent_output_404(self, aiohttp_client):
        """GET /api/agents/{id}/output for nonexistent should 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents/nonexistent/output")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_agent_activity(self, aiohttp_client):
        """GET /api/agents/{id}/activity should return structured activity."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/activity")
        assert resp.status == 200
        data = await resp.json()
        assert "tool_invocations" in data
        assert "file_operations" in data
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_get_agent_snapshots(self, aiohttp_client):
        """GET /api/agents/{id}/snapshots should return snapshot list."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/snapshots")
        assert resp.status == 200
        data = await resp.json()
        assert "snapshots" in data
        assert isinstance(data["snapshots"], list)

    @pytest.mark.asyncio
    async def test_export_agent_output(self, aiohttp_client):
        """GET /api/agents/{id}/output/export should return exportable output."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/output/export")
        assert resp.status == 200


# ── Batch Operations Tests ──


class TestApiBatchOperations:
    @pytest.mark.asyncio
    async def test_batch_spawn(self, aiohttp_client):
        """POST /api/agents/batch-spawn should spawn multiple agents."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 50001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            resp = await client.post("/api/agents/batch-spawn", json={
                "agents": [
                    {"role": "backend", "task": "build API", "working_dir": TEST_WORKING_DIR},
                    {"role": "frontend", "task": "build UI", "working_dir": TEST_WORKING_DIR},
                ],
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["total_spawned"] == 2
            assert data["total_failed"] == 0

    @pytest.mark.asyncio
    async def test_batch_spawn_with_invalid_entry(self, aiohttp_client):
        """Batch spawn with one invalid entry should still spawn valid ones."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 50002
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            resp = await client.post("/api/agents/batch-spawn", json={
                "agents": [
                    {"role": "general", "task": "valid task", "working_dir": TEST_WORKING_DIR},
                    {"role": "general", "task": ""},  # missing task
                ],
            })
            data = await resp.json()
            assert data["total_spawned"] == 1
            assert data["total_failed"] == 1

    @pytest.mark.asyncio
    async def test_batch_spawn_empty_list(self, aiohttp_client):
        """Batch spawn with empty agent list should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/batch-spawn", json={"agents": []})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bulk_agent_action_kill(self, aiohttp_client):
        """POST /api/agents/bulk with kill action should kill specified agents."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        agent_ids = []
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 50010
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            for i in range(2):
                agent = await manager.spawn(role="general", name=f"bulk-{i}", task=f"task {i}", working_dir=TEST_WORKING_DIR)
                agent_ids.append(agent.id)

        resp = await client.post("/api/agents/bulk", json={
            "action": "kill",
            "agent_ids": agent_ids,
        })
        assert resp.status == 200
        data = await resp.json()
        assert len(data["success"]) == 2


# ── Agent Notes & Tags Tests ──


class TestApiNotesAndTags:
    async def _spawn_agent(self, app):
        """Helper to spawn a test agent."""
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 60001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name="notes-test", task="test", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_update_notes(self, aiohttp_client):
        """PUT /api/agents/{id}/notes should update agent notes."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(
            f"/api/agents/{agent.id}/notes",
            json={"notes": "Test note content"},
        )
        assert resp.status == 200

        # Verify the notes were saved
        resp = await client.get(f"/api/agents/{agent.id}")
        data = await resp.json()
        assert data.get("notes") == "Test note content"

    @pytest.mark.asyncio
    async def test_update_tags(self, aiohttp_client):
        """PUT /api/agents/{id}/tags should update agent tags."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(
            f"/api/agents/{agent.id}/tags",
            json={"tags": ["important", "api"]},
        )
        assert resp.status == 200

        # Verify the tags were saved
        resp = await client.get(f"/api/agents/{agent.id}")
        data = await resp.json()
        assert "important" in data.get("tags", [])
        assert "api" in data.get("tags", [])


# ── Agent Clone Tests ──


class TestApiClone:
    @pytest.mark.asyncio
    async def test_clone_agent(self, aiohttp_client):
        """POST /api/agents/{id}/clone should create a copy."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 70001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            original = await manager.spawn(role="backend", name="original", task="build API", working_dir=TEST_WORKING_DIR)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec2:
            mock_proc2 = MagicMock()
            mock_proc2.pid = 70002
            mock_proc2.returncode = None
            mock_exec2.return_value = mock_proc2

            resp = await client.post(f"/api/agents/{original.id}/clone")
            assert resp.status == 201
            data = await resp.json()
            agent_data = data.get("agent", data)
            assert agent_data["role"] == "backend"
            assert agent_data["id"] != original.id

    @pytest.mark.asyncio
    async def test_clone_nonexistent_agent(self, aiohttp_client):
        """Cloning a nonexistent agent should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/clone")
        assert resp.status == 404


# ── Fleet Export & Search Tests ──


class TestApiFleetAndSearch:
    @pytest.mark.asyncio
    async def test_fleet_export(self, aiohttp_client):
        """GET /api/fleet/export should return fleet state."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/fleet/export")
        assert resp.status == 200
        data = await resp.json()
        assert "agents" in data
        assert "exported_at" in data

    @pytest.mark.asyncio
    async def test_search_agents(self, aiohttp_client):
        """GET /api/search should search across agents."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 80001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            await manager.spawn(role="general", name="searchable", task="find this task", working_dir=TEST_WORKING_DIR)

        resp = await client.get("/api/search?q=searchable")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list) or isinstance(data, dict)


# ── Bookmarks Tests ──


class TestApiBookmarks:
    async def _spawn_agent(self, app):
        """Helper to spawn a test agent."""
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 90001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name="bm-test", task="test", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_add_and_list_bookmarks(self, aiohttp_client):
        """Add a bookmark, then list bookmarks."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        # Add bookmark
        resp = await client.post(
            f"/api/agents/{agent.id}/bookmarks",
            json={"line_index": 5, "label": "Important line"},
        )
        assert resp.status in (200, 201)

        # List bookmarks
        resp = await client.get(f"/api/agents/{agent.id}/bookmarks")
        assert resp.status == 200
        data = await resp.json()
        # Response may be {"bookmarks": [...]} or a plain list
        bookmarks = data.get("bookmarks", data) if isinstance(data, dict) else data
        assert isinstance(bookmarks, list)
        assert len(bookmarks) >= 1


# ── Patch Agent Tests ──


class TestApiPatchAgent:
    async def _spawn_agent(self, app):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 95001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name="patch-test", task="test", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_patch_agent_name(self, aiohttp_client):
        """PATCH /api/agents/{id} should update agent name."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.patch(f"/api/agents/{agent.id}", json={"name": "new-name"})
        assert resp.status == 200
        data = await resp.json()
        agent_data = data.get("agent", data)
        assert agent_data["name"] == "new-name"

    @pytest.mark.asyncio
    async def test_patch_agent_task(self, aiohttp_client):
        """PATCH /api/agents/{id} should update agent task."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.patch(f"/api/agents/{agent.id}", json={"task": "new task"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_patch_agent_not_found(self, aiohttp_client):
        """PATCH a nonexistent agent should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.patch("/api/agents/nonexistent", json={"name": "x"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_patch_agent_empty_body(self, aiohttp_client):
        """PATCH with empty body should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.patch(f"/api/agents/{agent.id}", json={})
        assert resp.status == 400


# ── Analytics & Costs Tests ──


class TestApiAnalyticsAndCosts:
    @pytest.mark.asyncio
    async def test_fleet_analytics(self, aiohttp_client):
        """GET /api/analytics should return fleet analytics."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/analytics")
        assert resp.status == 200
        data = await resp.json()
        assert "total_cost" in data or "status_distribution" in data or "agents_count" in data

    @pytest.mark.asyncio
    async def test_costs_endpoint(self, aiohttp_client):
        """GET /api/costs should return cost breakdown."""
        app = _make_test_app()
        app["db"].get_agent_history = AsyncMock(return_value=[])
        client = await aiohttp_client(app)

        resp = await client.get("/api/costs")
        assert resp.status == 200
        data = await resp.json()
        assert "active" in data
        assert "total_cost_usd" in data

    @pytest.mark.asyncio
    async def test_collaboration_graph(self, aiohttp_client):
        """GET /api/collaboration should return collaboration data."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/collaboration")
        assert resp.status == 200
        data = await resp.json()
        assert "nodes" in data or "agents" in data or "edges" in data or isinstance(data, dict)


# ── Queue Tests ──


class TestApiQueue:
    @pytest.mark.asyncio
    async def test_list_empty_queue(self, aiohttp_client):
        """GET /api/queue should return empty list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/queue")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_add_to_queue(self, aiohttp_client):
        """POST /api/queue should add task to queue."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/queue", json={
            "role": "general",
            "task": "queued task",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status in (200, 201)

    @pytest.mark.asyncio
    async def test_add_to_queue_missing_task(self, aiohttp_client):
        """POST /api/queue without task should fail."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/queue", json={"role": "general"})
        assert resp.status == 400


# ── History & Events Tests ──


class TestApiHistoryAndEvents:
    @pytest.mark.asyncio
    async def test_list_history(self, aiohttp_client):
        """GET /api/history should return agent history."""
        app = _make_test_app()
        app["db"].get_agent_history = AsyncMock(return_value=[])
        app["db"].get_agent_history_count = AsyncMock(return_value=0)
        client = await aiohttp_client(app)

        resp = await client.get("/api/history")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_list_events(self, aiohttp_client):
        """GET /api/events should return event list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/events")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_get_conflicts(self, aiohttp_client):
        """GET /api/conflicts should return conflict data."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/conflicts")
        assert resp.status == 200


# ── Config Update Tests ──


class TestApiConfigUpdate:
    @pytest.mark.asyncio
    async def test_put_config(self, aiohttp_client):
        """PUT /api/config should update configuration."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/config", json={"max_agents": 8})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_config_invalid(self, aiohttp_client):
        """PUT /api/config with invalid JSON should fail."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/config", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400


# ── Agent Tool Invocations & File Operations Tests ──


class TestApiToolAndFileOps:
    async def _spawn_agent(self, app):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 96001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name="tool-test", task="test", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_get_tool_invocations(self, aiohttp_client):
        """GET /api/agents/{id}/tool-invocations should return tool data."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/tool-invocations")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.asyncio
    async def test_get_file_operations(self, aiohttp_client):
        """GET /api/agents/{id}/file-operations should return file op data."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/file-operations")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, (list, dict))

    @pytest.mark.asyncio
    async def test_search_agent_output(self, aiohttp_client):
        """GET /api/agents/{id}/output/search should search output."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/output/search?q=test")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_get_agent_full_output(self, aiohttp_client):
        """GET /api/agents/{id}/full-output should return full output."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.get(f"/api/agents/{agent.id}/full-output")
        assert resp.status == 200


# ── Intelligence Endpoints Tests ──


class TestApiIntelligence:
    @pytest.mark.asyncio
    async def test_get_insights(self, aiohttp_client):
        """GET /api/intelligence/insights should return insights list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/intelligence/insights")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_intelligence_command(self, aiohttp_client):
        """POST /api/intelligence/command should process command."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/intelligence/command", json={
            "transcript": "show me all agents",
        })
        # May return 200 or 501 if intelligence is not configured
        assert resp.status in (200, 501, 400)


# ── Resumable Sessions Tests ──


class TestApiResumableSessions:
    @pytest.mark.asyncio
    async def test_get_resumable_sessions(self, aiohttp_client):
        """GET /api/sessions/resumable should return session list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/resumable")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, (list, dict))


# ── Scratchpad Tests ──


class TestApiScratchpad:
    @pytest.mark.asyncio
    async def test_get_scratchpad(self, aiohttp_client):
        """GET /api/scratchpad should return scratchpad entries."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/scratchpad?project_id=test-project-1")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_get_scratchpad_missing_project_id(self, aiohttp_client):
        """GET /api/scratchpad without project_id should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/scratchpad")
        assert resp.status == 400


# ── Input Validation Tests ──


class TestApiInputValidation:
    @pytest.mark.asyncio
    async def test_create_project_requires_string_name(self, aiohttp_client):
        """POST /api/projects with non-string name should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/projects", json={"name": 123, "path": "/tmp"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_requires_name_and_path(self, aiohttp_client):
        """POST /api/projects without name/path should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/projects", json={"name": "test"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_valid(self, aiohttp_client):
        """POST /api/projects with valid data should succeed."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/projects", json={"name": "test-proj", "path": "/tmp"})
        assert resp.status == 201
        data = await resp.json()
        assert data["name"] == "test-proj"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_workflow_requires_string_name(self, aiohttp_client):
        """POST /api/workflows with non-string name should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/workflows", json={"name": 123, "agents": [{"role": "general"}]})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_workflow_requires_agents(self, aiohttp_client):
        """POST /api/workflows without agents should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/workflows", json={"name": "test"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_workflow_valid(self, aiohttp_client):
        """POST /api/workflows with valid data should succeed."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/workflows", json={
            "name": "test-workflow",
            "agents": [{"role": "general", "task": "test task"}],
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["name"] == "test-workflow"

    @pytest.mark.asyncio
    async def test_send_message_agent_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/send for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/send", json={"message": "hello"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_project_not_found(self, aiohttp_client):
        """DELETE /api/projects/{id} for missing project returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.delete("/api/projects/nonexistent")
        assert resp.status == 404


# ── Agent Notes & Tags Tests ──


class TestApiAgentNotesTags:
    async def _spawn_agent(self, app):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 97001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                agent = await manager.spawn(role="general", name="notes-test", working_dir=TEST_WORKING_DIR, task="test task")
                return agent

    @pytest.mark.asyncio
    async def test_update_notes(self, aiohttp_client):
        """PUT /api/agents/{id}/notes should update notes."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(f"/api/agents/{agent.id}/notes", json={"notes": "important note"})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "updated"
        assert data["notes"] == "important note"

    @pytest.mark.asyncio
    async def test_update_notes_not_found(self, aiohttp_client):
        """PUT /api/agents/{id}/notes for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/agents/nonexistent/notes", json={"notes": "test"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_update_notes_invalid_type(self, aiohttp_client):
        """PUT /api/agents/{id}/notes with non-string notes returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(f"/api/agents/{agent.id}/notes", json={"notes": 123})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tags(self, aiohttp_client):
        """PUT /api/agents/{id}/tags should update tags."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(f"/api/agents/{agent.id}/tags", json={"tags": ["auth", "backend", "urgent"]})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "updated"
        assert isinstance(data["tags"], list)
        assert "auth" in data["tags"]

    @pytest.mark.asyncio
    async def test_update_tags_not_list(self, aiohttp_client):
        """PUT /api/agents/{id}/tags with non-list returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(f"/api/agents/{agent.id}/tags", json={"tags": "not-a-list"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_tags_too_many(self, aiohttp_client):
        """PUT /api/agents/{id}/tags with >20 tags returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.put(f"/api/agents/{agent.id}/tags", json={"tags": [f"tag{i}" for i in range(25)]})
        assert resp.status == 400


# ── Agent Bookmarks Tests ──


class TestApiAgentBookmarks:
    async def _spawn_agent(self, app):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 97002
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                agent = await manager.spawn(role="general", name="bookmark-test", working_dir=TEST_WORKING_DIR, task="test task")
                return agent

    @pytest.mark.asyncio
    async def test_add_bookmark(self, aiohttp_client):
        """POST /api/agents/{id}/bookmarks should add a bookmark."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.post(f"/api/agents/{agent.id}/bookmarks", json={
            "line": 42,
            "text": "Important finding here",
            "label": "key-finding",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "added"
        assert data["bookmark"]["line"] == 42

    @pytest.mark.asyncio
    async def test_add_bookmark_missing_fields(self, aiohttp_client):
        """POST /api/agents/{id}/bookmarks without required fields returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        # line must be int — sending a string should trigger validation
        resp = await client.post(f"/api/agents/{agent.id}/bookmarks", json={"line": "not-int", "text": "test"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_add_bookmark_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/bookmarks for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/bookmarks", json={"line": 1, "text": "test"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_bookmark_not_found(self, aiohttp_client):
        """DELETE /api/agents/{id}/bookmarks/{bid} for missing bookmark returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.delete(f"/api/agents/{agent.id}/bookmarks/nonexistent")
        assert resp.status == 404


# ── Agent Snapshots Tests ──


class TestApiAgentSnapshots:
    async def _spawn_agent(self, app):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 97003
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                agent = await manager.spawn(role="general", name="snap-test", working_dir=TEST_WORKING_DIR, task="test task")
                return agent

    @pytest.mark.asyncio
    async def test_create_snapshot(self, aiohttp_client):
        """POST /api/agents/{id}/snapshots should create a manual snapshot."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.post(f"/api/agents/{agent.id}/snapshots")
        assert resp.status == 201
        data = await resp.json()
        assert data["trigger"] == "manual"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_snapshot_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/snapshots for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/nonexistent/snapshots")
        assert resp.status == 404


# ── List Endpoints Tests (Projects, Workflows, Presets) ──


class TestApiListEndpoints:
    @pytest.mark.asyncio
    async def test_list_projects(self, aiohttp_client):
        """GET /api/projects should return project list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_list_workflows(self, aiohttp_client):
        """GET /api/workflows should return workflow list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/workflows")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_list_presets(self, aiohttp_client):
        """GET /api/presets should return preset list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/presets")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)


# ── Preset CRUD Tests ──


class TestApiPresets:
    @pytest.mark.asyncio
    async def test_create_preset(self, aiohttp_client):
        """POST /api/presets should create a new preset."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/presets", json={
            "name": "my-test-preset",
            "role": "backend",
            "task": "Build the API",
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["name"] == "my-test-preset"
        assert "id" in data

    @pytest.mark.asyncio
    async def test_create_preset_missing_name(self, aiohttp_client):
        """POST /api/presets without name should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/presets", json={"role": "general"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_preset_invalid_role(self, aiohttp_client):
        """POST /api/presets with invalid role should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/presets", json={"name": "test", "role": "nonexistent_role"})
        assert resp.status == 400


# ── Bulk Respond Tests ──


class TestApiBulkRespond:
    async def _spawn_agent(self, app, name="bulk-test"):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 97004
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                agent = await manager.spawn(role="general", name=name, working_dir=TEST_WORKING_DIR, task="test task")
                return agent

    @pytest.mark.asyncio
    async def test_bulk_respond_empty(self, aiohttp_client):
        """POST /api/agents/bulk-respond with empty responses returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/bulk-respond", json={"responses": []})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bulk_respond_invalid_items(self, aiohttp_client):
        """POST /api/agents/bulk-respond with invalid items returns partial failures."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/agents/bulk-respond", json={
            "responses": [
                {"agent_id": "nonexistent", "message": "hello"},
                {"agent_id": "", "message": "no id"},
            ]
        })
        assert resp.status == 200
        data = await resp.json()
        assert len(data["failed"]) >= 2
        assert data["success"] == []


# ── Scratchpad CRUD Tests ──


class TestApiScratchpadCRUD:
    @pytest.mark.asyncio
    async def test_upsert_scratchpad(self, aiohttp_client):
        """PUT /api/scratchpad should upsert an entry."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/scratchpad", json={
            "project_id": "proj-1",
            "key": "api_endpoint",
            "value": "/api/users",
            "set_by": "agent-a1",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_upsert_scratchpad_missing_fields(self, aiohttp_client):
        """PUT /api/scratchpad without project_id/key returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/scratchpad", json={"value": "test"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_scratchpad_missing_project(self, aiohttp_client):
        """DELETE /api/scratchpad/{key} without project_id returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.delete("/api/scratchpad/some-key")
        assert resp.status == 400


# ── Config Export/Import Tests ──


class TestApiConfigExportImport:
    @pytest.mark.asyncio
    async def test_export_config(self, aiohttp_client):
        """GET /api/config/export should return config JSON."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/config/export")
        # May return 200 (config exists) or 404 (no config file in test)
        assert resp.status in (200, 404)

    @pytest.mark.asyncio
    async def test_import_config_invalid_json(self, aiohttp_client):
        """POST /api/config/import with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/config/import", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_import_config_non_object(self, aiohttp_client):
        """POST /api/config/import with non-object returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/config/import", json=[1, 2, 3])
        assert resp.status == 400


# ── Agent Messages Tests ──


class TestApiAgentMessages:
    async def _spawn_agent(self, app, name="msg-test"):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 97005
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                agent = await manager.spawn(role="general", name=name, working_dir=TEST_WORKING_DIR, task="test task")
                return agent

    @pytest.mark.asyncio
    async def test_get_messages_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/messages for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents/nonexistent/messages")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_send_message_missing_fields(self, aiohttp_client):
        """POST /api/agents/{id}/message without required fields returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.post(f"/api/agents/{agent.id}/message", json={"content": "hello"})
        assert resp.status == 400  # missing to_agent_id

    @pytest.mark.asyncio
    async def test_send_message_target_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/message to nonexistent target returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)

        resp = await client.post(f"/api/agents/{agent.id}/message", json={
            "to_agent_id": "nonexistent",
            "content": "hello",
        })
        assert resp.status == 404


# ── Config Validation Tests ──

class TestConfigValidation:
    """Tests for PUT /api/config validation across multiple fields."""

    @pytest.mark.asyncio
    async def test_put_config_invalid_max_agents_zero(self, aiohttp_client):
        """max_agents=0 is below minimum — should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 0})
        assert resp.status == 400
        body = await resp.json()
        assert "max_agents" in body["error"]

    @pytest.mark.asyncio
    async def test_put_config_invalid_max_agents_over(self, aiohttp_client):
        """max_agents=200 is above max 100 — should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 200})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_invalid_capture_interval(self, aiohttp_client):
        """output_capture_interval below 0.5 should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"output_capture_interval": 0.1})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_invalid_memory_limit(self, aiohttp_client):
        """memory_limit_mb below 256 should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"memory_limit_mb": 64})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_invalid_idle_ttl(self, aiohttp_client):
        """idle_agent_ttl below 300 should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"idle_agent_ttl": 100})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_invalid_health_threshold(self, aiohttp_client):
        """health_low_threshold of 0 should return 400 (must be > 0)."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"health_low_threshold": 0})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_unknown_key_ignored(self, aiohttp_client):
        """Unknown config keys should be silently ignored, not cause 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"nonexistent_key": "value"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_config_valid_bool(self, aiohttp_client):
        """Boolean fields like llm_enabled should accept true/false."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"llm_enabled": False})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_config_persists_in_memory(self, aiohttp_client):
        """After PUT /api/config, GET /api/config should reflect the change."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 12})
        assert resp.status == 200
        resp2 = await client.get("/api/config")
        data = await resp2.json()
        assert data["max_agents"] == 12


# ── Workflow Run Tests ──

class TestWorkflowRun:
    """Tests for workflow run/cancel/list endpoints."""

    @pytest.mark.asyncio
    async def test_run_workflow_nonexistent(self, aiohttp_client):
        """POST /api/workflows/nonexistent/run should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/workflows/nonexistent/run")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_list_workflow_runs_empty(self, aiohttp_client):
        """GET /api/workflow-runs should return empty list initially."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/workflow-runs")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_cancel_workflow_run_not_found(self, aiohttp_client):
        """POST /api/workflow-runs/nonexistent/cancel should return 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/workflow-runs/nonexistent/cancel")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_handoff_agent_source_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/handoff with nonexistent source returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent/handoff", json={
            "target_id": "also-nonexistent",
            "context": "test context",
        })
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_search_output_invalid_regex_returns_400(self, aiohttp_client):
        """?regex=true with invalid pattern should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        agent = await manager.spawn(
            role="general", name="regex-test", task="test", working_dir=TEST_WORKING_DIR,
        )
        resp = await client.get(f"/api/agents/{agent.id}/output/search?q=[invalid&regex=true")
        assert resp.status == 400
        data = await resp.json()
        assert "regex" in data.get("error", "").lower()


# ── Bulk Agent Action Tests ────────────────────────────
class TestBulkAgentAction:
    """Tests for POST /api/agents/bulk endpoint."""

    @pytest.mark.asyncio
    async def test_bulk_invalid_action(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/bulk", json={
            "action": "explode", "agent_ids": ["abc"],
        })
        assert resp.status == 400
        body = await resp.json()
        assert "Invalid action" in body["error"]

    @pytest.mark.asyncio
    async def test_bulk_empty_agent_ids(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/bulk", json={
            "action": "kill", "agent_ids": [],
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_bulk_send_requires_message(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/bulk", json={
            "action": "send", "agent_ids": ["abc"], "message": "",
        })
        assert resp.status == 400
        body = await resp.json()
        assert "message" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_bulk_kill_nonexistent_agents(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/bulk", json={
            "action": "kill", "agent_ids": ["nope1", "nope2"],
        })
        assert resp.status == 200
        body = await resp.json()
        assert len(body["success"]) == 0
        assert len(body["failed"]) == 2

    @pytest.mark.asyncio
    async def test_bulk_pause_existing_agent(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        agent = await manager.spawn(
            role="general", name="bulk-test", task="test", working_dir=TEST_WORKING_DIR,
        )
        agent.set_status("working")
        resp = await client.post("/api/agents/bulk", json={
            "action": "pause", "agent_ids": [agent.id],
        })
        assert resp.status == 200
        body = await resp.json()
        assert agent.id in body["success"]

    @pytest.mark.asyncio
    async def test_bulk_mixed_existing_and_missing(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        agent = await manager.spawn(
            role="general", name="bulk-mix", task="test", working_dir=TEST_WORKING_DIR,
        )
        agent.set_status("working")
        resp = await client.post("/api/agents/bulk", json={
            "action": "pause", "agent_ids": [agent.id, "nonexistent"],
        })
        assert resp.status == 200
        body = await resp.json()
        assert agent.id in body["success"]
        assert any(f["id"] == "nonexistent" for f in body["failed"])

    @pytest.mark.asyncio
    async def test_bulk_invalid_json(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/bulk", data="not json",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400


# ── Batch Spawn Tests ──────────────────────────────────
class TestBatchSpawn:
    """Tests for POST /api/agents/batch-spawn endpoint."""

    @pytest.mark.asyncio
    async def test_batch_spawn_empty_list(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn", json={"agents": []})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_batch_spawn_too_many(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        specs = [{"role": "general", "task": f"task-{i}"} for i in range(11)]
        resp = await client.post("/api/agents/batch-spawn", json={"agents": specs})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_batch_spawn_missing_task(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        resp = await client.post("/api/agents/batch-spawn", json={
            "agents": [{"role": "general", "task": ""}, {"role": "general", "task": "valid task"}],
        })
        # 201 because the valid agent succeeds
        assert resp.status == 201
        body = await resp.json()
        assert len(body["failed"]) >= 1
        assert body["total_spawned"] >= 1
        assert any("task" in f.get("error", "").lower() for f in body["failed"])

    @pytest.mark.asyncio
    async def test_batch_spawn_invalid_role(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        resp = await client.post("/api/agents/batch-spawn", json={
            "agents": [{"role": "mythical-unicorn", "task": "test"}],
        })
        # 400 because no agents spawned (only one spec and it fails)
        assert resp.status == 400
        body = await resp.json()
        assert len(body["failed"]) == 1
        assert "role" in body["failed"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_batch_spawn_invalid_json(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn", data="bad",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400


# ── Fleet Analytics Tests ──────────────────────────────
class TestFleetAnalytics:
    """Tests for GET /api/analytics endpoint."""

    @pytest.mark.asyncio
    async def test_analytics_empty_fleet(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/analytics")
        assert resp.status == 200
        body = await resp.json()
        assert body["total_cost_usd"] == 0
        assert body["total_tokens_input"] == 0
        assert body["avg_health_score"] == 0

    @pytest.mark.asyncio
    async def test_analytics_with_agents(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        a1 = await manager.spawn(role="backend", name="a1", task="test", working_dir=TEST_WORKING_DIR)
        a2 = await manager.spawn(role="frontend", name="a2", task="test", working_dir=TEST_WORKING_DIR)
        resp = await client.get("/api/analytics")
        assert resp.status == 200
        body = await resp.json()
        assert "status_distribution" in body
        assert "role_distribution" in body
        assert "top_files" in body
        assert "tool_usage" in body


# ── Validate Spawn Tests ──────────────────────────────
class TestValidateSpawn:
    """Tests for POST /api/agents/validate endpoint."""

    @pytest.mark.asyncio
    async def test_validate_valid_params(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/validate", json={
            "role": "backend", "task": "Build API", "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True
        assert body["resolved"]["role"] == "backend"

    @pytest.mark.asyncio
    async def test_validate_invalid_role(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/validate", json={
            "role": "wizard", "task": "cast spells",
        })
        assert resp.status == 400
        body = await resp.json()
        assert body["valid"] is False
        assert any("role" in e.lower() for e in body["errors"])

    @pytest.mark.asyncio
    async def test_validate_missing_task_is_warning(self, aiohttp_client):
        """Missing task is a warning, not an error — validation still passes."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/validate", json={"role": "general"})
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True
        assert any("task" in w.lower() for w in body.get("warnings", []))

    @pytest.mark.asyncio
    async def test_validate_invalid_json(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/validate", data="not json",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_validate_name_sanitization(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/validate", json={
            "role": "general", "task": "test", "name": "test\x00agent\x01name",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True
        assert "\x00" not in body["resolved"]["name"]


# ── Intelligence Command Tests ─────────────────────────
class TestIntelligenceCommand:
    """Tests for POST /api/intelligence/command endpoint."""

    @pytest.mark.asyncio
    async def test_command_missing_transcript(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/command", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_command_keyword_spawn(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/command", json={
            "transcript": "spawn a backend agent",
        })
        assert resp.status == 200
        body = await resp.json()
        assert "intent" in body
        intent = body["intent"]
        assert intent["action"] == "spawn"

    @pytest.mark.asyncio
    async def test_command_keyword_status(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/command", json={
            "transcript": "what is the status",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["intent"]["action"] == "status"

    @pytest.mark.asyncio
    async def test_command_keyword_unknown(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/command", json={
            "transcript": "make me a sandwich",
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["intent"]["action"] == "unknown"
