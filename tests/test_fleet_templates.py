"""Tests for Wave 4: Fleet Deploy & Templates.

Tests fleet template CRUD, task variable substitution, deploy endpoint,
and database operations.
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
from ashlr_ao.server import _substitute_task_vars
from conftest import make_mock_db, make_test_app

TEST_WORKING_DIR = str(Path.home())


# ═══════════════════════════════════════════════
# Task Variable Substitution
# ═══════════════════════════════════════════════


class TestSubstituteTaskVars:
    """Tests for _substitute_task_vars template engine."""

    def test_date_replacement(self):
        result = _substitute_task_vars("Deploy on {date}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in result

    def test_time_replacement(self):
        result = _substitute_task_vars("Check at {time}")
        assert "{time}" not in result

    def test_project_name(self):
        project = {"name": "auth-service", "path": "/home/user/auth"}
        result = _substitute_task_vars("Fix {project_name} tests", project)
        assert "auth-service" in result

    def test_branch_from_default(self):
        project = {"name": "api", "default_branch": "main"}
        result = _substitute_task_vars("Merge {branch} into staging", project)
        assert "main" in result

    def test_git_remote(self):
        project = {"name": "api", "git_remote_url": "git@github.com:org/api.git"}
        result = _substitute_task_vars("Clone from {git_remote}", project)
        assert "git@github.com:org/api.git" in result

    def test_no_project(self):
        result = _substitute_task_vars("Do {project_name} work")
        assert "{project_name}" in result  # Unreplaced without project

    def test_extra_vars(self):
        result = _substitute_task_vars("Fix {custom_var} issue", extra={"custom_var": "auth"})
        assert "auth" in result

    def test_multiple_replacements(self):
        project = {"name": "api", "default_branch": "develop"}
        result = _substitute_task_vars("Build {project_name} on {branch} at {date}", project)
        assert "api" in result
        assert "develop" in result
        assert "{date}" not in result

    def test_empty_task(self):
        result = _substitute_task_vars("")
        assert result == ""

    def test_no_vars_in_task(self):
        result = _substitute_task_vars("Just do the work")
        assert result == "Just do the work"


# ═══════════════════════════════════════════════
# Fleet Template API Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestFleetTemplateAPI:
    """Tests for fleet template CRUD endpoints."""

    async def test_list_empty(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet-templates")
            assert resp.status == 200
            data = await resp.json()
            assert data == []

    async def test_create_template(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "name": "Full Stack Team",
                "description": "Frontend + Backend + Tester",
                "agents": [
                    {"role": "frontend", "task": "Build UI"},
                    {"role": "backend", "task": "Build API"},
                    {"role": "tester", "task": "Write tests"},
                ],
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["name"] == "Full Stack Team"
            assert len(data["agents"]) == 3
            assert "id" in data

    async def test_create_missing_name(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "agents": [{"role": "general", "task": "test"}],
            })
            assert resp.status == 400

    async def test_create_empty_agents(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "name": "Empty",
                "agents": [],
            })
            assert resp.status == 400

    async def test_create_too_many_agents(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        agents = [{"role": "general", "task": f"task-{i}"} for i in range(21)]
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "name": "Too Many",
                "agents": agents,
            })
            assert resp.status == 400

    async def test_get_template(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        template = {
            "id": "tmpl1",
            "name": "Test",
            "description": "desc",
            "project_id": "",
            "agents": [{"role": "general", "task": "test"}],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet-templates/tmpl1")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "Test"

    async def test_get_template_not_found(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet-templates/nonexistent")
            assert resp.status == 404

    async def test_update_template(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        template = {
            "id": "tmpl1",
            "name": "Old Name",
            "description": "old",
            "project_id": "",
            "agents": [{"role": "general", "task": "test"}],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/fleet-templates/tmpl1", json={"name": "New Name"})
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "New Name"

    async def test_update_template_not_found(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/fleet-templates/nope", json={"name": "x"})
            assert resp.status == 404

    async def test_delete_template(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        app["db"].delete_fleet_template = AsyncMock(return_value=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/fleet-templates/tmpl1")
            assert resp.status == 200

    async def test_delete_template_not_found(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete("/api/fleet-templates/nope")
            assert resp.status == 404

    async def test_list_with_project_filter(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        app["db"].get_fleet_templates = AsyncMock(return_value=[
            {"id": "t1", "name": "Proj Template", "agents": []},
        ])
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/fleet-templates?project_id=proj1")
            assert resp.status == 200
            app["db"].get_fleet_templates.assert_awaited_with("proj1")


# ═══════════════════════════════════════════════
# Fleet Template Deploy Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestFleetTemplateDeploy:
    """Tests for the deploy endpoint."""

    async def test_deploy_template_not_found(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/nope/deploy", json={})
            assert resp.status == 404

    async def test_deploy_empty_agents(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        template = {
            "id": "t1", "name": "Empty",
            "agents": [],
            "project_id": "",
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/t1/deploy", json={})
            assert resp.status == 400

    async def test_deploy_exceeds_capacity(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        # Fill agents to max
        manager = app["agent_manager"]
        for i in range(100):
            manager.agents[f"a{i}"] = MagicMock()

        template = {
            "id": "t1", "name": "Big Team",
            "agents": [{"role": "general", "task": "test"}],
            "project_id": "",
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/t1/deploy", json={})
            assert resp.status == 409

    async def test_deploy_spawns_agents(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]

        template = {
            "id": "t1", "name": "Team",
            "agents": [
                {"role": "frontend", "task": "Build UI"},
                {"role": "backend", "task": "Build API"},
            ],
            "project_id": "",
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)

        mock_agent = MagicMock()
        mock_agent.to_dict = MagicMock(return_value={"id": "spawned1", "status": "spawning"})
        spawn_count = 0

        async def mock_spawn(**kwargs):
            nonlocal spawn_count
            spawn_count += 1
            aid = f"spawned{spawn_count}"
            manager.agents[aid] = mock_agent
            return aid

        manager.spawn = mock_spawn

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/t1/deploy", json={})
            assert resp.status == 201
            data = await resp.json()
            assert data["spawned"] == 2
            assert data["template"] == "Team"

    async def test_deploy_with_project(self):
        from aiohttp.test_utils import TestClient, TestServer
        from ashlr_ao.models import Project
        app = make_test_app()
        manager = app["agent_manager"]

        project = Project(
            id="proj1", name="API Service", path=TEST_WORKING_DIR,
            default_branch="main", default_backend="claude-code",
        )
        app["db"].get_project = AsyncMock(return_value=project)

        template = {
            "id": "t1", "name": "Team",
            "agents": [{"role": "general", "task": "Fix {project_name} on {branch}"}],
            "project_id": "proj1",
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)

        spawned_tasks = []
        mock_agent = MagicMock()
        mock_agent.to_dict = MagicMock(return_value={"id": "s1", "status": "spawning"})

        async def mock_spawn(**kwargs):
            spawned_tasks.append(kwargs.get("task", ""))
            manager.agents["s1"] = mock_agent
            return "s1"

        manager.spawn = mock_spawn

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/t1/deploy", json={})
            assert resp.status == 201
            # Task should have variables substituted
            assert len(spawned_tasks) == 1
            assert "API Service" in spawned_tasks[0]
            assert "main" in spawned_tasks[0]

    async def test_deploy_with_custom_vars(self):
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        manager = app["agent_manager"]

        template = {
            "id": "t1", "name": "Custom",
            "agents": [{"role": "general", "task": "Fix {ticket} on {service}"}],
            "project_id": "",
        }
        app["db"].get_fleet_template = AsyncMock(return_value=template)

        spawned_tasks = []
        mock_agent = MagicMock()
        mock_agent.to_dict = MagicMock(return_value={"id": "s1", "status": "spawning"})

        async def mock_spawn(**kwargs):
            spawned_tasks.append(kwargs.get("task", ""))
            manager.agents["s1"] = mock_agent
            return "s1"

        manager.spawn = mock_spawn

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates/t1/deploy", json={
                "vars": {"ticket": "AUTH-123", "service": "auth-api"},
            })
            assert resp.status == 201
            assert "AUTH-123" in spawned_tasks[0]
            assert "auth-api" in spawned_tasks[0]


# ═══════════════════════════════════════════════
# Fleet Template Database Tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestFleetTemplateDatabase:
    """Tests for fleet template database operations."""

    async def test_save_and_get(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()

        template = {
            "id": "tmpl1",
            "name": "Test Fleet",
            "description": "A test fleet",
            "project_id": "proj1",
            "agents": [
                {"role": "frontend", "task": "Build UI"},
                {"role": "backend", "task": "Build API"},
            ],
        }
        await db.save_fleet_template(template)

        result = await db.get_fleet_template("tmpl1")
        assert result is not None
        assert result["name"] == "Test Fleet"
        assert len(result["agents"]) == 2
        assert result["agents"][0]["role"] == "frontend"
        await db.close()

    async def test_list_all(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()

        for i in range(3):
            await db.save_fleet_template({
                "id": f"t{i}",
                "name": f"Template {i}",
                "agents": [{"role": "general", "task": f"Task {i}"}],
            })

        result = await db.get_fleet_templates()
        assert len(result) == 3
        await db.close()

    async def test_list_by_project(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()

        await db.save_fleet_template({
            "id": "t1", "name": "Global", "project_id": "",
            "agents": [{"role": "general", "task": "test"}],
        })
        await db.save_fleet_template({
            "id": "t2", "name": "Project-specific", "project_id": "proj1",
            "agents": [{"role": "backend", "task": "test"}],
        })
        await db.save_fleet_template({
            "id": "t3", "name": "Other project", "project_id": "proj2",
            "agents": [{"role": "frontend", "task": "test"}],
        })

        # Filter by proj1 should return global + proj1 templates
        result = await db.get_fleet_templates("proj1")
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert "Global" in names
        assert "Project-specific" in names
        await db.close()

    async def test_delete(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()

        await db.save_fleet_template({
            "id": "t1", "name": "Deletable",
            "agents": [{"role": "general", "task": "test"}],
        })
        assert await db.delete_fleet_template("t1") is True
        assert await db.get_fleet_template("t1") is None
        await db.close()

    async def test_delete_nonexistent(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()
        assert await db.delete_fleet_template("nope") is False
        await db.close()

    async def test_update(self):
        from ashlr_ao.database import Database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        db = Database(db_path)
        await db.init()

        await db.save_fleet_template({
            "id": "t1", "name": "Original",
            "agents": [{"role": "general", "task": "test"}],
        })
        await db.save_fleet_template({
            "id": "t1", "name": "Updated",
            "agents": [{"role": "backend", "task": "new task"}],
        })
        result = await db.get_fleet_template("t1")
        assert result["name"] == "Updated"
        assert result["agents"][0]["role"] == "backend"
        await db.close()

    async def test_no_db_returns_empty(self):
        from ashlr_ao.database import Database
        db = Database("/tmp/nonexistent.db")
        # Don't call init — simulate no DB
        result = await db.get_fleet_templates()
        assert result == []
        result = await db.get_fleet_template("t1")
        assert result is None
        assert await db.delete_fleet_template("t1") is False


# ═══════════════════════════════════════════════
# Audit hardening tests
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestFleetTemplateValidation:
    """Audit: validate agent specs in create_fleet_template."""

    async def test_create_template_agent_missing_task(self):
        """Agent specs missing 'task' should be rejected."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "name": "Bad Template",
                "agents": [{"role": "backend"}],  # no task
            })
            assert resp.status == 400
            data = await resp.json()
            assert "task" in data["error"]

    async def test_create_template_agent_not_dict(self):
        """Agent specs that aren't dicts should be rejected."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/fleet-templates", json={
                "name": "Bad Template",
                "agents": ["not a dict"],
            })
            assert resp.status == 400
            data = await resp.json()
            assert "dict" in data["error"]

    async def test_create_template_task_truncated(self):
        """Long task strings should be truncated to 10K chars."""
        from aiohttp.test_utils import TestClient, TestServer
        app = make_test_app()
        async with TestClient(TestServer(app)) as client:
            long_task = "x" * 20000
            resp = await client.post("/api/fleet-templates", json={
                "name": "Truncate Test",
                "agents": [{"role": "backend", "task": long_task}],
            })
            assert resp.status == 201
            data = await resp.json()
            assert len(data["agents"][0]["task"]) == 10000


class TestSubstituteTaskVarsHardening:
    """Audit: control chars stripped from git_remote in substitution."""

    def test_strips_control_chars_from_git_remote(self):
        from ashlr_ao.server import _substitute_task_vars
        project = {
            "name": "test",
            "git_remote_url": "https://github.com/org/repo\nignore this",
            "default_branch": "main",
        }
        result = _substitute_task_vars("Push to {git_remote}", project)
        assert "\n" not in result
        assert "ignore this" in result  # content preserved, just newline stripped
        assert "https://github.com/org/repo" in result

    def test_strips_null_bytes(self):
        from ashlr_ao.server import _substitute_task_vars
        project = {
            "name": "test",
            "git_remote_url": "https://github.com/org/repo\x00evil",
            "default_branch": "main",
        }
        result = _substitute_task_vars("Push to {git_remote}", project)
        assert "\x00" not in result
