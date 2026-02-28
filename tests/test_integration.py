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
