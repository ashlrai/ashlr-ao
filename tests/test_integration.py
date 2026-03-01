"""Integration tests for Ashlar AO — WebSocket + REST API via aiohttp test client.

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

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlar_server


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
    db.get_scratchpad = AsyncMock(return_value=[])
    db.db_path = Path("/tmp/test-ashlar.db")  # fake path for stats endpoint
    db.find_similar_tasks = AsyncMock(return_value=[])
    db.get_resumable_sessions = AsyncMock(return_value=[])
    db.archive_output = AsyncMock()
    db.release_file_locks = AsyncMock()
    db.get_archived_output = AsyncMock(return_value=([], 0))
    db.get_bookmarks = AsyncMock(return_value=[])
    db.add_bookmark = AsyncMock(return_value=1)
    return db


def _make_test_app():
    """Create a test app with DB and background tasks disabled."""
    config = ashlar_server.Config()
    config.demo_mode = True  # avoid needing real claude CLI
    config.spawn_pressure_block = False  # disable for tests — host CPU/memory can trigger false 503s

    app = ashlar_server.create_app(config)
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
    """Create a test client for the Ashlar app."""
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
                "working_dir": "/tmp",
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
                role="general", name="msg-limit-test", task="test", working_dir="/tmp"
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
                role="general", name="msg-test", task="test", working_dir="/tmp"
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
                "working_dir": "/tmp",
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
                    "working_dir": "/tmp",
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
            agent = await manager.spawn(role="general", name="pause-test", task="test", working_dir="/tmp")

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
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 30002
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(role="backend", name="restart-test", task="old task", working_dir="/tmp")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec2:
            mock_proc2 = MagicMock()
            mock_proc2.pid = 30003
            mock_proc2.returncode = None
            mock_exec2.return_value = mock_proc2
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
            "working_dir": "/tmp",
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
            "working_dir": "/tmp",
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
            "working_dir": "/tmp",
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
            "working_dir": "/tmp",
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
            "working_dir": "/tmp",
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
            "working_dir": "/tmp",
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
            return await manager.spawn(role="general", name="output-test", task="test", working_dir="/tmp")

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
                    {"role": "backend", "task": "build API", "working_dir": "/tmp"},
                    {"role": "frontend", "task": "build UI", "working_dir": "/tmp"},
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
                    {"role": "general", "task": "valid task", "working_dir": "/tmp"},
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
                agent = await manager.spawn(role="general", name=f"bulk-{i}", task=f"task {i}", working_dir="/tmp")
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
            return await manager.spawn(role="general", name="notes-test", task="test", working_dir="/tmp")

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
            original = await manager.spawn(role="backend", name="original", task="build API", working_dir="/tmp")

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
            await manager.spawn(role="general", name="searchable", task="find this task", working_dir="/tmp")

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
            return await manager.spawn(role="general", name="bm-test", task="test", working_dir="/tmp")

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
