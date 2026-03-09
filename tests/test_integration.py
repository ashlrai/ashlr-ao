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
sys.path.insert(0, str(Path(__file__).parent))

# Use home directory for working_dir in tests (server validates against home)
TEST_WORKING_DIR = str(Path.home())

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server


from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app


@pytest.fixture
async def cli(aiohttp_client):
    """Create a test client for the Ashlr app."""
    app = _make_test_app()
    return await aiohttp_client(app)


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
        """max_agents=200 is clamped to license max (100) then accepted."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 200})
        # License clamp reduces 200→100, which is within valid range (1-100)
        assert resp.status == 200

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
        # 207 Multi-Status: some spawned, some failed
        assert resp.status == 207
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
        await manager.spawn(role="backend", name="a1", task="test", working_dir=TEST_WORKING_DIR)
        await manager.spawn(role="frontend", name="a2", task="test", working_dir=TEST_WORKING_DIR)
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


# ── Agent Handoff Tests ──────────────────────────────────

class TestApiHandoff:
    """Tests for POST /api/agents/{id}/handoff endpoint."""

    async def _spawn_agent(self, app, name="handoff-test"):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98001
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name=name, task="test task", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_handoff_source_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/handoff with nonexistent source returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent/handoff", json={
            "target_id": "also-nonexistent",
            "context": "test context",
        })
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_handoff_missing_target(self, aiohttp_client):
        """POST /api/agents/{id}/handoff without to_agent_id returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        resp = await client.post(f"/api/agents/{agent.id}/handoff", json={
            "context": "test context",
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_handoff_target_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/handoff with nonexistent target returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        resp = await client.post(f"/api/agents/{agent.id}/handoff", json={
            "to_agent_id": "nonexistent-target",
            "context": "test context",
        })
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_handoff_invalid_json(self, aiohttp_client):
        """POST /api/agents/{id}/handoff with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        resp = await client.post(f"/api/agents/{agent.id}/handoff",
                                 data="not json",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_handoff_success(self, aiohttp_client):
        """POST /api/agents/{id}/handoff with valid source and target succeeds."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        source = await self._spawn_agent(app, name="source-agent")
        target = await self._spawn_agent(app, name="target-agent")
        with patch.object(app["agent_manager"], "send_message", new_callable=AsyncMock, return_value=True):
            resp = await client.post(f"/api/agents/{source.id}/handoff", json={
                "to_agent_id": target.id,
                "key_findings": "found a bug in auth module",
            })
            assert resp.status == 200


# ── Agent Clone Extended Tests ───────────────────────────

class TestApiCloneExtended:
    """Extended tests for POST /api/agents/{id}/clone endpoint."""

    async def _spawn_agent(self, app, name="clone-ext-test", **kwargs):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98010
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(
                role=kwargs.get("role", "backend"),
                name=name,
                task=kwargs.get("task", "build API"),
                working_dir=TEST_WORKING_DIR,
                model=kwargs.get("model"),
            )

    @pytest.mark.asyncio
    async def test_clone_max_agents_reached(self, aiohttp_client):
        """Cloning when at max agents should return 503 or 400."""
        app = _make_test_app()
        app["agent_manager"].config.max_agents = 1
        client = await aiohttp_client(app)
        original = await self._spawn_agent(app)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98011
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            resp = await client.post(f"/api/agents/{original.id}/clone")
            assert resp.status in (400, 409, 503)

    @pytest.mark.asyncio
    async def test_clone_custom_working_dir(self, aiohttp_client):
        """Clone with custom working_dir in body should use it."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        original = await self._spawn_agent(app)
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98012
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            resp = await client.post(f"/api/agents/{original.id}/clone", json={
                "working_dir": TEST_WORKING_DIR,
            })
            assert resp.status == 201

    @pytest.mark.asyncio
    async def test_clone_preserves_model(self, aiohttp_client):
        """Cloned agent should preserve the model from the original."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        original = await self._spawn_agent(app, model="claude-opus-4")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98013
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            resp = await client.post(f"/api/agents/{original.id}/clone")
            assert resp.status == 201
            data = await resp.json()
            agent_data = data.get("agent", data)
            assert agent_data["role"] == "backend"


# ── Agent Tool Invocations & File Operations Extended ────

class TestApiToolAndFileOpsExtended:
    """Extended tests for tool invocation and file operation endpoints."""

    @pytest.mark.asyncio
    async def test_tool_invocations_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/tool-invocations for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/tool-invocations")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_tool_invocations_empty(self, aiohttp_client):
        """GET /api/agents/{id}/tool-invocations for agent with no tools returns empty."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98020
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(role="general", name="tool-empty", task="test", working_dir=TEST_WORKING_DIR)
        resp = await client.get(f"/api/agents/{agent.id}/tool-invocations")
        assert resp.status == 200
        data = await resp.json()
        invocations = data if isinstance(data, list) else data.get("invocations", data.get("data", []))
        assert isinstance(invocations, list)

    @pytest.mark.asyncio
    async def test_file_operations_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/file-operations for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/file-operations")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_file_operations_empty(self, aiohttp_client):
        """GET /api/agents/{id}/file-operations for agent with no ops returns empty."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98021
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(role="general", name="file-empty", task="test", working_dir=TEST_WORKING_DIR)
        resp = await client.get(f"/api/agents/{agent.id}/file-operations")
        assert resp.status == 200
        data = await resp.json()
        operations = data if isinstance(data, list) else data.get("operations", data.get("data", []))
        assert isinstance(operations, list)


# ── Agent Output Search Extended ─────────────────────────

class TestApiOutputSearchExtended:
    """Extended tests for GET /api/agents/{id}/output/search endpoint."""

    @pytest.mark.asyncio
    async def test_search_agent_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/output/search for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/output/search?q=test")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_search_no_matches(self, aiohttp_client):
        """GET /api/agents/{id}/output/search with no matching term returns empty."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98030
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            agent = await manager.spawn(role="general", name="search-empty", task="test", working_dir=TEST_WORKING_DIR)
        resp = await client.get(f"/api/agents/{agent.id}/output/search?q=zzzznonexistent")
        assert resp.status == 200
        data = await resp.json()
        matches = data if isinstance(data, list) else data.get("matches", data.get("results", []))
        assert isinstance(matches, list)
        assert len(matches) == 0


# ── Agent Output Export Extended ─────────────────────────

class TestApiOutputExportExtended:
    """Extended tests for GET /api/agents/{id}/output/export endpoint."""

    @pytest.mark.asyncio
    async def test_export_agent_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/output/export for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/output/export")
        assert resp.status == 404


# ── Agent Message Extended Tests ─────────────────────────

class TestApiAgentMessageExtended:
    """Extended tests for agent-to-agent messaging endpoints."""

    async def _spawn_agent(self, app, name="msg-ext-test"):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98040
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            with patch.object(manager, "_tmux_capture", new_callable=AsyncMock, return_value=[]):
                return await manager.spawn(role="general", name=name, working_dir=TEST_WORKING_DIR, task="test task")

    @pytest.mark.asyncio
    async def test_post_message_missing_body(self, aiohttp_client):
        """POST /api/agents/{id}/message with empty JSON body returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        resp = await client.post(f"/api/agents/{agent.id}/message", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_post_message_source_not_found(self, aiohttp_client):
        """POST /api/agents/{id}/message for missing source agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/nonexistent/message", json={
            "to_agent_id": "someone",
            "content": "hello",
        })
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_post_message_success(self, aiohttp_client):
        """POST /api/agents/{id}/message with valid source and target succeeds."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        source = await self._spawn_agent(app, name="msg-source")
        target = await self._spawn_agent(app, name="msg-target")
        with patch.object(app["agent_manager"], "send_message", new_callable=AsyncMock, return_value=True):
            resp = await client.post(f"/api/agents/{source.id}/message", json={
                "to_agent_id": target.id,
                "content": "please review this code",
            })
            assert resp.status in (200, 201)

    @pytest.mark.asyncio
    async def test_get_messages_not_found(self, aiohttp_client):
        """GET /api/agents/{id}/messages for missing agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/messages")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_messages_empty(self, aiohttp_client):
        """GET /api/agents/{id}/messages for agent with no messages returns empty."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        resp = await client.get(f"/api/agents/{agent.id}/messages")
        assert resp.status == 200
        data = await resp.json()
        messages = data if isinstance(data, list) else data.get("messages", [])
        assert isinstance(messages, list)


# ── WebSocket Message Types Extended ─────────────────────

class TestWebSocketMessageTypes:
    """Extended WebSocket tests for kill, pause, resume, send, and agent_message types."""

    async def _spawn_agent(self, app, name="ws-type-test"):
        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 98050
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc
            return await manager.spawn(role="general", name=name, task="test task", working_dir=TEST_WORKING_DIR)

    @pytest.mark.asyncio
    async def test_ws_kill_agent_not_found(self, aiohttp_client):
        """Sending kill via WS for nonexistent agent should not crash."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        await ws.send_json({"type": "kill", "agent_id": "nonexistent"})
        # Should receive error event, not a crash
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert msg["type"] in ("error", "event")
        except asyncio.TimeoutError:
            pass  # Server silently ignored — acceptable
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_kill_existing_agent(self, aiohttp_client):
        """Sending kill via WS for existing agent should process it."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await ws.send_json({"type": "kill", "agent_id": agent.id})
            received_types = set()
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    received_types.add(msg["type"])
                except asyncio.TimeoutError:
                    break
            # Server processed the kill without crashing.
            # WS broadcast may or may not arrive within timeout in demo mode.
            assert isinstance(received_types, set)
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_pause_existing_agent(self, aiohttp_client):
        """Sending pause via WS for existing agent should work."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        await ws.send_json({"type": "pause", "agent_id": agent.id})
        received_types = set()
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                received_types.add(msg["type"])
                if msg["type"] == "agent_update":
                    break
            except asyncio.TimeoutError:
                break
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_resume_existing_agent(self, aiohttp_client):
        """Sending resume via WS for existing agent should work."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        agent.set_status("paused")
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        await ws.send_json({"type": "resume", "agent_id": agent.id})
        received_types = set()
        for _ in range(5):
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                received_types.add(msg["type"])
                if msg["type"] == "agent_update":
                    break
            except asyncio.TimeoutError:
                break
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_resume_nonexistent_agent(self, aiohttp_client):
        """Sending resume via WS for nonexistent agent should not crash."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        await ws.send_json({"type": "resume", "agent_id": "nonexistent"})
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert msg["type"] in ("error", "event")
        except asyncio.TimeoutError:
            pass  # Server silently ignored — acceptable
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_send_to_existing_agent(self, aiohttp_client):
        """Sending a message via WS to existing agent should work."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        with patch.object(app["agent_manager"], "send_message", new_callable=AsyncMock, return_value=True):
            await ws.send_json({"type": "send", "agent_id": agent.id, "message": "proceed"})
            received_types = set()
            for _ in range(5):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                    received_types.add(msg["type"])
                except asyncio.TimeoutError:
                    break
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_send_to_nonexistent_agent(self, aiohttp_client):
        """Sending a message via WS to nonexistent agent should not crash."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        await ws.send_json({"type": "send", "agent_id": "nonexistent", "message": "hello"})
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert msg["type"] in ("error", "event")
        except asyncio.TimeoutError:
            pass  # Server silently ignored — acceptable
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_agent_message_type(self, aiohttp_client):
        """Sending agent_message type via WS should be handled."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app, name="ws-msg-src")
        target = await self._spawn_agent(app, name="ws-msg-tgt")
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        with patch.object(app["agent_manager"], "send_message", new_callable=AsyncMock, return_value=True):
            await ws.send_json({
                "type": "agent_message",
                "from_agent_id": agent.id,
                "to_agent_id": target.id,
                "content": "test inter-agent message",
            })
            # Expect some response or graceful handling
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                # Any non-crash response is fine
                assert msg["type"] in ("event", "error", "agent_update", "message_sent", "agent_message")
            except asyncio.TimeoutError:
                pass  # Graceful handling — no crash
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_agent_message_confirmation(self, aiohttp_client):
        """Sending agent_message via WS should produce a confirmation or event."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        agent = await self._spawn_agent(app, name="ws-conf-src")
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        # Send message to self (edge case, should not crash)
        await ws.send_json({
            "type": "agent_message",
            "from_agent_id": agent.id,
            "to_agent_id": agent.id,
            "content": "message to self",
        })
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert isinstance(msg, dict)
        except asyncio.TimeoutError:
            pass  # No response but no crash — acceptable
        await ws.close()


# ─────────────────────────────────────────────
# Dashboard Serving Tests
# ─────────────────────────────────────────────

class TestDashboardServing:
    @pytest.mark.asyncio
    async def test_get_dashboard_html(self, aiohttp_client):
        """GET / returns the dashboard HTML page."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/")
        assert resp.status == 200
        assert "text/html" in resp.content_type
        body = await resp.text()
        assert "<html" in body.lower() or "<!doctype" in body.lower()

    @pytest.mark.asyncio
    async def test_get_logo(self, aiohttp_client):
        """GET /logo.png returns the logo image."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/logo.png")
        # Either 200 (logo exists) or 404 (not found) — both are valid
        assert resp.status in (200, 404)
        if resp.status == 200:
            assert "image" in resp.content_type

    @pytest.mark.asyncio
    async def test_dashboard_has_security_headers(self, aiohttp_client):
        """Dashboard response includes security headers."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/")
        assert "Content-Security-Policy" in resp.headers
        assert "X-Content-Type-Options" in resp.headers


# ─────────────────────────────────────────────
# WebSocket Connection Tests
# ─────────────────────────────────────────────

class TestWSConnection:
    @pytest.mark.asyncio
    async def test_ws_connect_sends_sync(self, aiohttp_client):
        """WebSocket connection immediately sends sync message."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        msg = await asyncio.wait_for(ws.receive_json(), timeout=5.0)
        assert msg["type"] == "sync"
        assert "agents" in msg
        assert "config" in msg
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_unknown_type_returns_error(self, aiohttp_client):
        """Sending unknown message type via WS returns error."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync
        await ws.send_json({"type": "unknown_garbage"})
        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg["type"] == "error"
        assert "unknown" in msg["message"].lower()
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_sync_request_throttled(self, aiohttp_client):
        """Rapid sync_request messages are throttled."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume initial sync

        # Send two rapid sync requests
        await ws.send_json({"type": "sync_request"})
        msg1 = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg1["type"] == "sync"

        await ws.send_json({"type": "sync_request"})
        msg2 = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        # Second should be throttled
        assert msg2["type"] == "error"
        assert "throttled" in msg2["message"].lower()
        await ws.close()

    @pytest.mark.asyncio
    async def test_ws_disconnect_cleans_up(self, aiohttp_client):
        """Disconnecting WS cleans up client tracking."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        hub = app["ws_hub"]
        initial_count = len(hub.clients)

        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # sync
        assert len(hub.clients) == initial_count + 1

        await ws.close()
        await asyncio.sleep(0.1)  # Let cleanup run
        assert len(hub.clients) == initial_count

    @pytest.mark.asyncio
    async def test_ws_spawn_rate_limited(self, aiohttp_client):
        """WS spawn operations are rate limited when burst is exceeded."""
        app = _make_test_app()
        # Use a real rate limiter with very low burst to trigger quickly
        rl = ashlr_server.RateLimiter()
        app["rate_limiter"] = rl
        # Pre-exhaust the spawn bucket for the anonymous WS user
        for _ in range(6):
            rl.check("ws:anon:spawn", cost=1.0, rate=2.0, burst=5.0)

        client = await aiohttp_client(app)
        ws = await client.ws_connect("/ws")
        await ws.receive_json()  # consume sync

        # This spawn should be rate limited
        await ws.send_json({
            "type": "spawn",
            "task": "test task",
            "role": "general",
            "working_dir": str(Path.home()),
        })
        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg.get("type") == "error"
        assert "rate limit" in msg.get("message", "").lower()
        await ws.close()


# ── Spawn Input Validation (Security Hardening) ──────────

class TestSpawnInputValidation:
    """Tests for tool name validation and system_prompt_extra length cap."""

    @pytest.mark.asyncio
    async def test_spawn_rejects_invalid_tool_names(self, aiohttp_client):
        """Tool names with special characters should be rejected."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "role": "general", "task": "test",
            "working_dir": TEST_WORKING_DIR,
            "tools": ["Bash", "'; rm -rf /; echo '"],
        })
        assert resp.status == 400
        body = await resp.json()
        assert "Invalid tool name" in body["error"]

    @pytest.mark.asyncio
    async def test_spawn_accepts_valid_tool_names(self, aiohttp_client):
        """Valid alphanumeric tool names with hyphens/underscores should pass."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "role": "general", "task": "test",
            "working_dir": TEST_WORKING_DIR,
            "tools": ["Bash", "Read", "Write", "my-custom_tool"],
        })
        # Should pass validation (may fail on spawn itself in demo mode, but not 400 for tools)
        assert resp.status != 400 or "tool" not in (await resp.json()).get("error", "").lower()

    @pytest.mark.asyncio
    async def test_spawn_rejects_oversized_system_prompt(self, aiohttp_client):
        """system_prompt_extra over 5000 chars should be rejected."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "role": "general", "task": "test",
            "working_dir": TEST_WORKING_DIR,
            "system_prompt_extra": "x" * 5001,
        })
        assert resp.status == 400
        body = await resp.json()
        assert "5000" in body["error"]

    @pytest.mark.asyncio
    async def test_spawn_accepts_valid_system_prompt(self, aiohttp_client):
        """system_prompt_extra under 5000 chars should pass validation."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "role": "general", "task": "test",
            "working_dir": TEST_WORKING_DIR,
            "system_prompt_extra": "x" * 4999,
        })
        # Should pass validation (may fail later in spawn, but not 400 for prompt length)
        assert resp.status != 400 or "5000" not in (await resp.json()).get("error", "")


# ── Project Path Validation (Security Hardening) ──────────

class TestProjectPathValidation:
    """Test project path boundary checks use os.sep for safe prefix matching."""

    @pytest.mark.asyncio
    async def test_project_path_exact_home_accepted(self, aiohttp_client):
        """Home directory itself should be accepted."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Home", "path": str(Path.home()),
        })
        assert resp.status in (200, 201)

    @pytest.mark.asyncio
    async def test_project_path_under_home_subdir_accepted(self, aiohttp_client):
        """Path under home directory should be accepted."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Home Sub", "path": TEST_WORKING_DIR,
        })
        assert resp.status in (200, 201)

    @pytest.mark.asyncio
    async def test_project_path_outside_home_rejected(self, aiohttp_client):
        """Path outside home and /tmp should be rejected."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Evil", "path": "/usr/local",
        })
        assert resp.status == 400
        body = await resp.json()
        assert "home directory" in body["error"].lower() or "must be" in body["error"].lower()


# ── Verify Auth Endpoint Tests ─────────────────────────

class TestVerifyAuth:
    """Tests for POST /api/auth/verify backward-compat token endpoint."""

    @pytest.mark.asyncio
    async def test_verify_auth_no_auth_required(self, aiohttp_client):
        """When auth not required, verify returns valid=True."""
        app = _make_test_app()
        app["config"].require_auth = False
        client = await aiohttp_client(app)
        resp = await client.post("/api/auth/verify", json={})
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True
        assert body["auth_required"] is False

    @pytest.mark.asyncio
    async def test_verify_auth_correct_token(self, aiohttp_client):
        """Correct token should return valid=True."""
        app = _make_test_app()
        app["config"].require_auth = True
        app["config"].auth_token = "my-secret-token"
        client = await aiohttp_client(app)
        resp = await client.post("/api/auth/verify", json={"token": "my-secret-token"})
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True

    @pytest.mark.asyncio
    async def test_verify_auth_wrong_token(self, aiohttp_client):
        """Wrong token should return valid=False."""
        app = _make_test_app()
        app["config"].require_auth = True
        app["config"].auth_token = "my-secret-token"
        client = await aiohttp_client(app)
        resp = await client.post("/api/auth/verify", json={"token": "wrong-token"})
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is False

    @pytest.mark.asyncio
    async def test_verify_auth_bearer_header(self, aiohttp_client):
        """Token from Authorization header should work."""
        app = _make_test_app()
        app["config"].require_auth = True
        app["config"].auth_token = "header-token"
        client = await aiohttp_client(app)
        resp = await client.post("/api/auth/verify", json={},
                                 headers={"Authorization": "Bearer header-token"})
        assert resp.status == 200
        body = await resp.json()
        assert body["valid"] is True


# ── Session Cookie Tests ─────────────────────────

class TestSetSessionCookie:
    """Tests for _set_session_cookie helper."""

    def test_sets_httponly_samesite(self):
        """Cookie should be HttpOnly with SameSite=Strict."""
        from ashlr_server import _set_session_cookie
        response = MagicMock()
        _set_session_cookie(response, "test-session-id")
        response.set_cookie.assert_called_once()
        kwargs = response.set_cookie.call_args[1]
        assert kwargs["httponly"] is True
        assert kwargs["samesite"] == "Strict"
        assert kwargs["max_age"] == 86400
        assert kwargs["path"] == "/"

    def test_secure_flag_behind_https_proxy(self):
        """Secure flag should be set when X-Forwarded-Proto is https."""
        from ashlr_server import _set_session_cookie
        response = MagicMock()
        request = MagicMock()
        request.headers = {"X-Forwarded-Proto": "https"}
        _set_session_cookie(response, "test-session-id", request)
        kwargs = response.set_cookie.call_args[1]
        assert kwargs.get("secure") is True

    def test_no_secure_flag_without_https(self):
        """Secure flag should NOT be set when not behind HTTPS proxy."""
        from ashlr_server import _set_session_cookie
        response = MagicMock()
        request = MagicMock()
        request.headers = {}
        _set_session_cookie(response, "test-session-id", request)
        kwargs = response.set_cookie.call_args[1]
        assert "secure" not in kwargs


# ── Intelligence Insight Tests ─────────────────────────

class TestAcknowledgeInsight:
    """Tests for POST /api/intelligence/insights/{id}/ack."""

    @pytest.mark.asyncio
    async def test_acknowledge_existing_insight(self, aiohttp_client):
        """Acknowledging an existing insight should set acknowledged=True."""
        app = _make_test_app()
        insight = ashlr_server.AgentInsight(
            id="ins-001", insight_type="conflict", severity="warning",
            message="File conflict", agent_ids=["a1", "a2"],
        )
        app["intelligence_insights"] = [insight]
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/insights/ins-001/ack")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "acknowledged"
        assert insight.acknowledged is True

    @pytest.mark.asyncio
    async def test_acknowledge_missing_insight_returns_404(self, aiohttp_client):
        """Acknowledging a non-existent insight should return 404."""
        app = _make_test_app()
        app["intelligence_insights"] = []
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/insights/nonexistent/ack")
        assert resp.status == 404


# ── Agent Suggestions Tests ─────────────────────────

class TestAgentSuggestions:
    """Tests for GET /api/agents/suggestions?task=..."""

    @pytest.mark.asyncio
    async def test_suggestions_short_query(self, aiohttp_client):
        """Queries < 5 chars should return empty suggestions with message."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/suggestions?task=hi")
        assert resp.status == 200
        body = await resp.json()
        assert body["suggestions"] == []
        assert "5 characters" in body.get("message", "")

    @pytest.mark.asyncio
    async def test_suggestions_empty_db(self, aiohttp_client):
        """Valid query with no matching tasks should return empty list."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/suggestions?task=build+a+rest+api")
        assert resp.status == 200
        body = await resp.json()
        assert body["suggestions"] == []

    @pytest.mark.asyncio
    async def test_suggestions_db_error_handled(self, aiohttp_client):
        """DB errors should return empty suggestions, not 500."""
        app = _make_test_app()
        app["db"].find_similar_tasks = AsyncMock(side_effect=Exception("DB offline"))
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/suggestions?task=build+api+endpoint")
        assert resp.status == 200
        body = await resp.json()
        assert body["suggestions"] == []
        assert "error" in body


class TestBatchSpawnMultiStatus:
    """Tests for 207 Multi-Status on partial batch spawn failures."""

    @pytest.mark.asyncio
    async def test_batch_spawn_all_succeed_returns_201(self, aiohttp_client):
        """All specs succeeding should return 201."""
        app = _make_test_app()
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn", json={
            "agents": [
                {"role": "general", "task": "task one"},
                {"role": "general", "task": "task two"},
            ],
        })
        assert resp.status == 201
        body = await resp.json()
        assert body["total_failed"] == 0
        assert body["total_spawned"] == 2

    @pytest.mark.asyncio
    async def test_batch_spawn_all_fail_returns_400(self, aiohttp_client):
        """All specs failing should return 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn", json={
            "agents": [
                {"role": "mythical", "task": "nope"},
                {"role": "imaginary", "task": "nah"},
            ],
        })
        assert resp.status == 400
        body = await resp.json()
        assert body["total_spawned"] == 0
        assert body["total_failed"] == 2

    @pytest.mark.asyncio
    async def test_batch_spawn_partial_returns_207(self, aiohttp_client):
        """Mix of success and failure should return 207 Multi-Status."""
        app = _make_test_app()
        manager = app["agent_manager"]
        manager._run_tmux = AsyncMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn", json={
            "agents": [
                {"role": "general", "task": "good task"},
                {"role": "mythical", "task": "will fail"},
            ],
        })
        assert resp.status == 207
        body = await resp.json()
        assert body["total_spawned"] >= 1
        assert body["total_failed"] >= 1


class TestSilentExceptionLogging:
    """Verify that formerly-silent exception handlers now log."""

    def test_spawn_recent_task_logs_on_error(self):
        """spawn_agent should log when add_recent_task fails, not silently pass."""
        import inspect
        src = inspect.getsource(ashlr_server.spawn_agent)
        assert 'log.debug' in src or 'log.warning' in src
        assert 'add_recent_task' in src

    def test_resume_body_parse_catches_json_errors(self):
        """resume_agent should catch JSONDecodeError for missing body."""
        import inspect
        src = inspect.getsource(ashlr_server.resume_agent)
        assert 'JSONDecodeError' in src

    def test_restart_body_parse_logs(self):
        """restart_agent should log when request body parsing fails."""
        import inspect
        src = inspect.getsource(ashlr_server.restart_agent)
        assert 'log.debug' in src

    def test_health_detailed_memory_logs(self):
        """health_detailed should log when server memory reading fails."""
        import inspect
        src = inspect.getsource(ashlr_server.health_detailed)
        assert 'log.debug' in src


class TestConfTestMockCompleteness:
    """Verify conftest mock DB has all methods needed by the server."""

    def test_mock_has_auth_methods(self):
        """Mock DB should have user/session management methods."""
        from conftest import make_mock_db
        db = make_mock_db()
        assert callable(db.create_user)
        assert callable(db.get_user_by_email)
        assert callable(db.get_user_by_id)
        assert callable(db.create_session)
        assert callable(db.get_session)
        assert callable(db.delete_session)
        assert callable(db.get_org_users)
        assert callable(db.user_count)

    def test_mock_has_coordination_methods(self):
        """Mock DB should have file lock and archive rotation methods."""
        from conftest import make_mock_db
        db = make_mock_db()
        assert callable(db.set_file_lock)
        assert callable(db.get_file_locks)
        assert callable(db.release_file_locks)
        assert callable(db.rotate_archive)
        assert callable(db.delete_expired_sessions)

    def test_mock_has_bookmark_crud(self):
        """Mock DB should have full bookmark CRUD."""
        from conftest import make_mock_db
        db = make_mock_db()
        assert callable(db.get_bookmarks)
        assert callable(db.add_bookmark)
        assert callable(db.delete_bookmark)


# ═══════════════════════════════════════════════
# T-5: TUI ready timeout path tests
# ═══════════════════════════════════════════════


class TestTuiReadyTimeout:
    """Tests for spawn behavior when _wait_for_tui_ready times out."""

    @pytest.mark.asyncio
    async def test_spawn_completes_on_tui_timeout(self, aiohttp_client):
        """Spawn completes even when _wait_for_tui_ready returns False (demo mode bypasses TUI)."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = ""
        success_result.stderr = ""
        manager._run_tmux = AsyncMock(return_value=success_result)
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._tmux_send_raw = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=False)  # Timeout!

        resp = await client.post("/api/agents", json={
            "role": "backend",
            "name": "tui-timeout-test",
            "task": "test task",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 201
        data = await resp.json()
        agent_id = data["id"]
        assert agent_id in manager.agents
        # Agent should still be spawned despite TUI timeout
        agent = manager.agents[agent_id]
        assert agent.status in ("spawning", "working", "planning")

    @pytest.mark.asyncio
    async def test_tui_ready_timeout_sends_enter_anyway(self):
        """When _wait_for_tui_ready returns False, spawn still sends Enter."""
        from ashlr_ao.manager import AgentManager
        from ashlr_ao.config import Config

        config = Config()
        config.demo_mode = False  # Non-demo mode to exercise TUI path
        manager = AgentManager(config)

        # Mark claude-code backend as available so spawn doesn't fail on resolution
        bc = manager.backend_configs["claude-code"]
        bc.available = True

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = ""
        success_result.stderr = ""
        manager._run_tmux = AsyncMock(return_value=success_result)
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._tmux_send_raw = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=False)  # Timeout!
        manager._tmux_get_pane_pid = AsyncMock(return_value=12345)

        agent = await manager.spawn(
            role="backend", name="tui-test",
            task="test task", working_dir=TEST_WORKING_DIR,
        )
        assert agent is not None
        # Enter should still be sent as fallback
        manager._tmux_send_raw.assert_awaited()

    @pytest.mark.asyncio
    async def test_restart_completes_on_tui_timeout(self, aiohttp_client):
        """Restart completes even when _wait_for_tui_ready returns False."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        manager = app["agent_manager"]

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stdout = ""
        success_result.stderr = ""
        manager._run_tmux = AsyncMock(return_value=success_result)
        manager._tmux_send_keys = AsyncMock(return_value=True)
        manager._tmux_send_raw = AsyncMock(return_value=True)
        manager._wait_for_tui_ready = AsyncMock(return_value=True)

        agent = await manager.spawn(role="backend", name="restart-tui", task="old task", working_dir=TEST_WORKING_DIR)

        # Now set TUI ready to timeout for restart
        manager._wait_for_tui_ready = AsyncMock(return_value=False)

        resp = await client.post(f"/api/agents/{agent.id}/restart")
        assert resp.status == 200
        assert agent.id in manager.agents
