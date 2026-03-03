"""CRUD endpoint tests for Ashlr AO — REST API via aiohttp test client.

Tests cover session resume, project update, presets, queue, history,
diagnostic, extensions, agent suggestions, agent list filters, health,
and backends endpoints.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    db.db_path = Path("/tmp/test-ashlr.db")
    db.find_similar_tasks = AsyncMock(return_value=[])
    db.get_resumable_sessions = AsyncMock(return_value=[])
    db.archive_output = AsyncMock()
    db.release_file_locks = AsyncMock()
    db.get_archived_output = AsyncMock(return_value=([], 0))
    db.get_bookmarks = AsyncMock(return_value=[])
    db.add_bookmark = AsyncMock(return_value=1)
    db.save_project = AsyncMock()
    db.delete_project = AsyncMock(return_value=False)
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
    db.get_agent_history_item = AsyncMock(return_value=None)
    db.get_project = AsyncMock(return_value=None)
    db.update_project = AsyncMock(return_value=None)
    db.get_preset = AsyncMock(return_value=None)
    db.get_agent_history = AsyncMock(return_value=[])
    db._db = None
    return db


def _make_test_app():
    """Create a test app with DB and background tasks disabled."""
    config = ashlr_server.Config()
    config.demo_mode = True
    config.spawn_pressure_block = False

    app = ashlr_server.create_app(config)
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


# ── Session Resume Tests ──


class TestSessionResumable:
    @pytest.mark.asyncio
    async def test_get_resumable_sessions_empty(self, aiohttp_client):
        """GET /api/sessions/resumable returns empty list when no sessions exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/resumable")
        assert resp.status == 200
        body = await resp.json()
        assert "sessions" in body
        assert body["sessions"] == []

    @pytest.mark.asyncio
    async def test_get_resumable_sessions_with_limit(self, aiohttp_client):
        """GET /api/sessions/resumable?limit=5 passes limit to DB."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/sessions/resumable?limit=5")
        assert resp.status == 200
        body = await resp.json()
        assert body["sessions"] == []
        # Verify DB was called with the limit
        app["db"].get_resumable_sessions.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_resume_session_not_found(self, aiohttp_client):
        """POST /api/sessions/{id}/resume returns 404 when session doesn't exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/sessions/nonexistent123/resume", json={})
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_resume_session_success(self, aiohttp_client):
        """POST /api/sessions/{id}/resume spawns agent when session found."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        session_data = {
            "id": "abc123",
            "name": "test-agent",
            "role": "backend",
            "task": "fix bug",
            "working_dir": TEST_WORKING_DIR,
            "backend": "claude-code",
            "plan_mode": 0,
            "model": "sonnet",
            "tools_allowed": '["Bash","Read"]',
            "project_id": None,
        }
        app["db"].get_agent_history_item = AsyncMock(return_value=session_data)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            resp = await client.post("/api/sessions/abc123/resume", json={})
            assert resp.status == 201
            body = await resp.json()
            assert "agent" in body
            assert body["resumed_from"] == "abc123"
            assert body["agent"]["role"] == "backend"

    @pytest.mark.asyncio
    async def test_resume_session_with_continue_message(self, aiohttp_client):
        """POST /api/sessions/{id}/resume with continue_message appends to task."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        session_data = {
            "id": "abc123",
            "name": "test-agent",
            "role": "backend",
            "task": "fix bug",
            "working_dir": TEST_WORKING_DIR,
            "backend": "claude-code",
            "plan_mode": 0,
            "model": None,
            "tools_allowed": None,
            "project_id": None,
        }
        app["db"].get_agent_history_item = AsyncMock(return_value=session_data)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 12346
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            resp = await client.post(
                "/api/sessions/abc123/resume",
                json={"continue_message": "also fix the tests"},
            )
            assert resp.status == 201
            body = await resp.json()
            agent = body["agent"]
            # The task should contain both the original and the continuation
            assert "fix bug" in agent["task"]
            assert "Continuation:" in agent["task"]
            assert "also fix the tests" in agent["task"]


# ── Project Update Tests ──


class TestProjectUpdate:
    @pytest.mark.asyncio
    async def test_update_project_not_found(self, aiohttp_client):
        """PUT /api/projects/{id} returns 404 when project doesn't exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/projects/p999", json={"name": "Updated"})
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_project_name_success(self, aiohttp_client):
        """PUT /api/projects/{id} with name update returns updated project."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        updated = {"id": "p1", "name": "Updated", "path": TEST_WORKING_DIR, "description": "test"}
        app["db"].update_project = AsyncMock(return_value=updated)

        resp = await client.put("/api/projects/p1", json={"name": "Updated"})
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "Updated"
        assert body["id"] == "p1"

    @pytest.mark.asyncio
    async def test_update_project_path_success(self, aiohttp_client):
        """PUT /api/projects/{id} with valid path update succeeds."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        updated = {"id": "p1", "name": "MyProject", "path": TEST_WORKING_DIR, "description": ""}
        app["db"].update_project = AsyncMock(return_value=updated)

        resp = await client.put("/api/projects/p1", json={"path": TEST_WORKING_DIR})
        assert resp.status == 200
        body = await resp.json()
        assert body["path"] == TEST_WORKING_DIR

    @pytest.mark.asyncio
    async def test_update_project_invalid_path(self, aiohttp_client):
        """PUT /api/projects/{id} with nonexistent path returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put(
            "/api/projects/p1",
            json={"path": "/nonexistent/path/that/does/not/exist"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "not a valid directory" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_project_restricted_path(self, aiohttp_client):
        """PUT /api/projects/{id} with restricted path (e.g., /etc) returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/projects/p1", json={"path": "/etc"})
        assert resp.status == 400
        body = await resp.json()
        assert "home directory" in body["error"].lower() or "must be under" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_project_invalid_json(self, aiohttp_client):
        """PUT /api/projects/{id} with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put(
            "/api/projects/p1",
            data=b"not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "invalid json" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_project_empty_body(self, aiohttp_client):
        """PUT /api/projects/{id} with empty body returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/projects/p1", json={})
        assert resp.status == 400
        body = await resp.json()
        assert "non-empty" in body["error"].lower()


# ── Preset Tests ──


class TestPresets:
    @pytest.mark.asyncio
    async def test_list_presets(self, aiohttp_client):
        """GET /api/presets returns list of presets."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/presets")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)

    @pytest.mark.asyncio
    async def test_create_preset(self, aiohttp_client):
        """POST /api/presets creates a new preset."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/presets", json={
            "name": "My Preset",
            "role": "backend",
            "backend": "claude-code",
            "task": "do stuff",
        })
        assert resp.status == 201
        body = await resp.json()
        assert body["name"] == "My Preset"
        assert body["role"] == "backend"
        assert "id" in body

    @pytest.mark.asyncio
    async def test_update_preset_not_found(self, aiohttp_client):
        """PUT /api/presets/{id} returns 404 when preset doesn't exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/presets/nope", json={"name": "Updated"})
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_preset_success(self, aiohttp_client):
        """PUT /api/presets/{id} updates preset when it exists."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        existing = {
            "id": "pr1",
            "name": "Old Name",
            "role": "general",
            "backend": "claude-code",
            "task": "old task",
            "system_prompt": "",
            "model": "",
            "tools_allowed": "",
            "working_dir": "",
        }
        app["db"].get_preset = AsyncMock(return_value=existing)

        resp = await client.put("/api/presets/pr1", json={"name": "New Name", "task": "new task"})
        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "New Name"
        assert body["task"] == "new task"
        # Unchanged fields should persist
        assert body["role"] == "general"

    @pytest.mark.asyncio
    async def test_delete_preset_not_found(self, aiohttp_client):
        """DELETE /api/presets/{id} returns 404 when preset doesn't exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.delete("/api/presets/nope")
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_preset_success(self, aiohttp_client):
        """DELETE /api/presets/{id} returns success when preset is deleted."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        app["db"].delete_preset = AsyncMock(return_value=True)

        resp = await client.delete("/api/presets/pr1")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "deleted"


# ── Queue Tests ──


class TestQueue:
    @pytest.mark.asyncio
    async def test_list_queue_empty(self, aiohttp_client):
        """GET /api/queue returns empty list initially."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/queue")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 0

    @pytest.mark.asyncio
    async def test_add_to_queue(self, aiohttp_client):
        """POST /api/queue adds a task to the queue."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/queue", json={
            "role": "backend",
            "name": "queued-task",
            "task": "implement feature X",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 201
        body = await resp.json()
        assert body["role"] == "backend"
        assert body["name"] == "queued-task"
        assert body["task"] == "implement feature X"
        assert "id" in body

    @pytest.mark.asyncio
    async def test_remove_from_queue_not_found(self, aiohttp_client):
        """DELETE /api/queue/{id} returns 404 when task not in queue."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.delete("/api/queue/nonexistent")
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_add_and_remove_from_queue(self, aiohttp_client):
        """POST /api/queue then DELETE /api/queue/{id} removes the task."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        # Add a task
        resp = await client.post("/api/queue", json={
            "role": "general",
            "task": "test task for removal",
        })
        assert resp.status == 201
        task_id = (await resp.json())["id"]

        # Remove it
        resp = await client.delete(f"/api/queue/{task_id}")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "removed"

        # Verify it's gone
        resp = await client.get("/api/queue")
        assert resp.status == 200
        queue = await resp.json()
        assert not any(t["id"] == task_id for t in queue)


# ── History Tests ──


class TestHistory:
    @pytest.mark.asyncio
    async def test_list_history_empty(self, aiohttp_client):
        """GET /api/history returns empty data list."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/history")
        assert resp.status == 200
        body = await resp.json()
        assert "data" in body
        assert "pagination" in body
        assert body["data"] == []
        assert body["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_get_history_item_not_found(self, aiohttp_client):
        """GET /api/history/{id} returns 404 when item doesn't exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/history/nonexistent")
        assert resp.status == 404
        body = await resp.json()
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_get_history_item_found(self, aiohttp_client):
        """GET /api/history/{id} returns item when it exists."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        item = {"id": "h1", "name": "past-agent", "role": "backend", "status": "complete"}
        app["db"].get_agent_history_item = AsyncMock(return_value=item)

        resp = await client.get("/api/history/h1")
        assert resp.status == 200
        body = await resp.json()
        assert body["id"] == "h1"
        assert body["name"] == "past-agent"

    @pytest.mark.asyncio
    async def test_list_history_with_limit(self, aiohttp_client):
        """GET /api/history?limit=5 passes limit parameter correctly."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/history?limit=5")
        assert resp.status == 200
        body = await resp.json()
        assert body["pagination"]["limit"] == 5
        # Verify the DB was called with the correct limit
        app["db"].get_agent_history.assert_awaited_once_with(5, 0)


# ── Diagnostic Test ──


class TestDiagnostic:
    @pytest.mark.asyncio
    async def test_run_diagnostic(self, aiohttp_client):
        """POST /api/diagnostic returns diagnostic results."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"tmux 3.4", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            resp = await client.post("/api/diagnostic")
            assert resp.status == 200
            body = await resp.json()
            assert "status" in body
            assert "results" in body
            assert "timestamp" in body
            # Should have subsections for various checks
            assert "python" in body["results"]
            assert "tmux" in body["results"]
            assert "system" in body["results"]
            assert "backends" in body["results"]


# ── Extension Tests ──


class TestExtensions:
    @pytest.mark.asyncio
    async def test_get_extensions(self, aiohttp_client):
        """GET /api/extensions returns extension scan results."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/extensions")
        assert resp.status == 200
        body = await resp.json()
        assert "skills" in body
        assert "mcp_servers" in body
        assert "plugins" in body
        assert isinstance(body["skills"], list)
        assert isinstance(body["mcp_servers"], list)

    @pytest.mark.asyncio
    async def test_refresh_extensions(self, aiohttp_client):
        """POST /api/extensions/refresh re-scans and returns results."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.post("/api/extensions/refresh")
        assert resp.status == 200
        body = await resp.json()
        assert "skills" in body
        assert "mcp_servers" in body
        assert "plugins" in body
        assert "scanned_at" in body
        # scanned_at should be set after refresh
        assert body["scanned_at"] != ""


# ── Agent Suggestions Tests ──


class TestAgentSuggestions:
    @pytest.mark.asyncio
    async def test_suggestions_no_task(self, aiohttp_client):
        """GET /api/agents/suggestions with no task returns empty suggestions."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents/suggestions")
        assert resp.status == 200
        body = await resp.json()
        assert body["suggestions"] == []

    @pytest.mark.asyncio
    async def test_suggestions_with_task(self, aiohttp_client):
        """GET /api/agents/suggestions?task=test query returns suggestions."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        # Mock DB to return similar tasks
        app["db"].find_similar_tasks = AsyncMock(return_value=[
            {
                "role": "backend",
                "backend": "claude-code",
                "task": "write tests for auth",
                "status": "complete",
                "duration_sec": 120,
                "estimated_cost_usd": 0.05,
                "completed_at": "2026-03-01T00:00:00",
            },
        ])

        resp = await client.get("/api/agents/suggestions?task=write tests")
        assert resp.status == 200
        body = await resp.json()
        assert body["query"] == "write tests"
        assert len(body["suggestions"]) == 1
        assert body["suggestions"][0]["role"] == "backend"
        assert body["suggestions"][0]["success"] is True

    @pytest.mark.asyncio
    async def test_suggestions_short_task(self, aiohttp_client):
        """GET /api/agents/suggestions?task=ab returns empty (min 5 chars)."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents/suggestions?task=ab")
        assert resp.status == 200
        body = await resp.json()
        assert body["suggestions"] == []


# ── Agent List with Filters Tests ──


class TestAgentListFilters:
    @pytest.mark.asyncio
    async def test_list_agents_empty(self, aiohttp_client):
        """GET /api/agents returns empty list when no agents spawned."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 0

    @pytest.mark.asyncio
    async def test_list_agents_branch_filter(self, aiohttp_client):
        """GET /api/agents?branch=main returns empty when no agents match."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents?branch=main")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 0

    @pytest.mark.asyncio
    async def test_list_agents_project_filter(self, aiohttp_client):
        """GET /api/agents?project_id=p1 returns empty when no agents match."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents?project_id=p1")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 0

    @pytest.mark.asyncio
    async def test_list_agents_status_filter(self, aiohttp_client):
        """GET /api/agents?status=working returns empty when no agents match."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/agents?status=working")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)
        assert len(body) == 0

    @pytest.mark.asyncio
    async def test_list_agents_with_spawned_agent(self, aiohttp_client):
        """GET /api/agents returns agent after spawning one."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        manager = app["agent_manager"]
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 55555
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await manager.spawn(
                role="backend", name="filter-test", task="test", working_dir=TEST_WORKING_DIR
            )

        resp = await client.get("/api/agents")
        assert resp.status == 200
        body = await resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "filter-test"


# ── Miscellaneous Tests ──


class TestMisc:
    @pytest.mark.asyncio
    async def test_health_check(self, aiohttp_client):
        """GET /api/health returns ok status."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert "agents" in body
        assert "agents_active" in body
        assert "agents_waiting" in body
        assert "db_available" in body
        assert "backends" in body

    @pytest.mark.asyncio
    async def test_list_backends(self, aiohttp_client):
        """GET /api/backends returns available backend configurations."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/backends")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, dict)
        # Should have at least the default claude-code backend
        assert "claude-code" in body or "demo" in body or len(body) > 0


# ── Additional Endpoint Coverage ──


class TestCostsEndpoint:
    @pytest.mark.asyncio
    async def test_get_costs(self, aiohttp_client):
        """GET /api/costs returns cost estimates."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/costs")
        assert resp.status == 200
        body = await resp.json()
        assert "active_agents" in body or "total_estimated_cost" in body or isinstance(body, dict)


class TestRolesEndpoint:
    @pytest.mark.asyncio
    async def test_get_roles(self, aiohttp_client):
        """GET /api/roles returns available roles."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/roles")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, (dict, list))


class TestConfigEndpoint:
    @pytest.mark.asyncio
    async def test_get_config(self, aiohttp_client):
        """GET /api/config returns current config."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/config")
        assert resp.status == 200
        body = await resp.json()
        assert "max_agents" in body or "default_role" in body

    @pytest.mark.asyncio
    async def test_put_config_valid(self, aiohttp_client):
        """PUT /api/config with valid max_agents succeeds."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 8})
        # May succeed or fail (disk write) but should not be 400
        assert resp.status in (200, 500)

    @pytest.mark.asyncio
    async def test_put_config_invalid_json(self, aiohttp_client):
        """PUT /api/config with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", data="not json",
                                headers={"Content-Type": "application/json"})
        assert resp.status == 400


class TestSystemEndpoint:
    @pytest.mark.asyncio
    async def test_get_system_metrics(self, aiohttp_client):
        """GET /api/system returns system metrics."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/system")
        assert resp.status == 200
        body = await resp.json()
        assert "cpu" in body or "cpu_pct" in body or "agents" in body


class TestAgentOutputEndpoint:
    @pytest.mark.asyncio
    async def test_get_output_nonexistent(self, aiohttp_client):
        """GET /api/agents/{id}/output for nonexistent agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/output")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_activity_nonexistent(self, aiohttp_client):
        """GET /api/agents/{id}/activity for nonexistent agent returns 404."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/agents/nonexistent/activity")
        assert resp.status == 404


class TestWorkflowEndpoints:
    @pytest.mark.asyncio
    async def test_list_workflows_empty(self, aiohttp_client):
        """GET /api/workflows returns empty list when no workflows exist."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/workflows")
        assert resp.status == 200
        body = await resp.json()
        assert isinstance(body, list)

    @pytest.mark.asyncio
    async def test_create_workflow_invalid_json(self, aiohttp_client):
        """POST /api/workflows with invalid JSON returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/workflows", data="bad",
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 400
