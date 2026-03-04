"""Tests for Wave 1: Project-Centric Foundation.

Covers Project dataclass, DB migrations, CRUD, git detection,
project context endpoint, project-aware spawn, and recent tasks.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

with patch("psutil.cpu_percent", return_value=0.0):
    from ashlr_server import Database, Agent, Config
    from ashlr_ao.models import Project

from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app

TEST_WORKING_DIR = str(Path.home())


# ─────────────────────────────────────────────
# Project Dataclass
# ─────────────────────────────────────────────

class TestProjectDataclass:
    def test_create_project_minimal(self):
        p = Project(id="p001", name="My Repo", path="/tmp/repo")
        assert p.id == "p001"
        assert p.name == "My Repo"
        assert p.path == "/tmp/repo"
        assert p.git_remote_url == ""
        assert p.default_branch == ""
        assert p.tags == []
        assert p.favorite is False
        assert p.recent_tasks == []
        assert p.auto_approve_patterns == []

    def test_create_project_full(self):
        p = Project(
            id="p002", name="Ashlr", path="/tmp/ashlr",
            description="Agent orchestrator",
            git_remote_url="https://github.com/user/ashlr.git",
            default_branch="main",
            default_backend="claude-code",
            default_model="opus",
            default_role="backend",
            tags=["python", "ai"],
            favorite=True,
            recent_tasks=[{"task": "Fix bug", "role": "backend"}],
            auto_approve_patterns=[{"pattern": "proceed", "response": "yes"}],
        )
        assert p.default_backend == "claude-code"
        assert p.default_model == "opus"
        assert p.tags == ["python", "ai"]
        assert p.favorite is True
        assert len(p.recent_tasks) == 1

    def test_to_dict(self):
        p = Project(
            id="p003", name="Test", path="/tmp/test",
            git_remote_url="git@github.com:user/test.git",
            default_branch="dev",
            tags=["go"],
            favorite=True,
        )
        d = p.to_dict()
        assert d["id"] == "p003"
        assert d["name"] == "Test"
        assert d["git_remote_url"] == "git@github.com:user/test.git"
        assert d["default_branch"] == "dev"
        assert d["tags"] == ["go"]
        assert d["favorite"] is True
        assert isinstance(d["recent_tasks"], list)
        assert isinstance(d["auto_approve_patterns"], list)

    def test_to_dict_returns_list_copies(self):
        """Ensure to_dict returns copies of mutable lists."""
        p = Project(id="p004", name="X", path="/tmp/x", tags=["a"])
        d = p.to_dict()
        d["tags"].append("b")
        assert p.tags == ["a"]  # Original unchanged

    def test_to_dict_all_keys_present(self):
        p = Project(id="p005", name="Y", path="/tmp/y")
        d = p.to_dict()
        expected_keys = {
            "id", "name", "path", "description", "created_at", "updated_at",
            "org_id", "git_remote_url", "default_branch", "default_backend",
            "default_model", "default_role", "tags", "favorite",
            "recent_tasks", "auto_approve_patterns",
        }
        assert set(d.keys()) == expected_keys

    def test_default_factory_isolation(self):
        """Each Project gets its own list instances."""
        p1 = Project(id="p1", name="A", path="/tmp/a")
        p2 = Project(id="p2", name="B", path="/tmp/b")
        p1.tags.append("x")
        assert p2.tags == []

    def test_empty_defaults(self):
        p = Project(id="p006", name="Z", path="/tmp/z")
        assert p.description == ""
        assert p.org_id == ""
        assert p.default_backend == ""
        assert p.default_model == ""
        assert p.default_role == ""


# ─────────────────────────────────────────────
# Database: Schema Migration
# ─────────────────────────────────────────────

class TestProjectSchemaMigration:
    @pytest.mark.asyncio
    async def test_init_creates_project_columns(self):
        """Verify new project columns exist after init."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        async with db._db.execute("PRAGMA table_info(projects)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        assert "git_remote_url" in columns
        assert "default_branch" in columns
        assert "default_backend" in columns
        assert "default_model" in columns
        assert "default_role" in columns
        assert "tags_json" in columns
        assert "favorite" in columns
        assert "recent_tasks_json" in columns
        assert "auto_approve_patterns_json" in columns
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_migration_idempotent(self):
        """Running init twice should not fail."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.close()
        db = Database(db_path)
        await db.init()
        # Should still work
        projects = await db.get_projects()
        assert isinstance(projects, list)
        await db.close()
        db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Database: Project CRUD with New Fields
# ─────────────────────────────────────────────

class TestProjectCRUD:
    @pytest.mark.asyncio
    async def test_save_and_get_project_with_new_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        project = {
            "id": "proj1", "name": "TestProject", "path": "/tmp/test",
            "git_remote_url": "https://github.com/user/repo.git",
            "default_branch": "main",
            "default_backend": "claude-code",
            "default_model": "opus",
            "default_role": "backend",
            "tags": ["python", "web"],
            "favorite": True,
        }
        await db.save_project(project)
        projects = await db.get_projects()
        assert len(projects) == 1
        p = projects[0]
        assert p["git_remote_url"] == "https://github.com/user/repo.git"
        assert p["default_branch"] == "main"
        assert p["default_backend"] == "claude-code"
        assert p["default_model"] == "opus"
        assert p["default_role"] == "backend"
        assert p["tags"] == ["python", "web"]
        assert p["favorite"] is True
        assert p["recent_tasks"] == []
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_save_project_minimal(self):
        """Save with only required fields — new fields should default."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "p1", "name": "Mini", "path": "/tmp/m"})
        p = await db.get_project("p1")
        assert p is not None
        assert p["git_remote_url"] == ""
        assert p["tags"] == []
        assert p["favorite"] is False
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_get_project_by_id(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "abc", "name": "ABC", "path": "/tmp/abc",
                               "git_remote_url": "https://example.com/abc.git"})
        p = await db.get_project("abc")
        assert p is not None
        assert p["name"] == "ABC"
        assert p["git_remote_url"] == "https://example.com/abc.git"

        missing = await db.get_project("nonexistent")
        assert missing is None
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_update_project_new_fields(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "u1", "name": "Update", "path": "/tmp/u"})
        updated = await db.update_project("u1", {
            "default_backend": "codex",
            "default_model": "sonnet",
            "default_role": "tester",
            "tags": ["rust"],
            "favorite": True,
            "git_remote_url": "https://github.com/user/u.git",
        })
        assert updated is not None
        assert updated["default_backend"] == "codex"
        assert updated["default_model"] == "sonnet"
        assert updated["default_role"] == "tester"
        assert updated["tags"] == ["rust"]
        assert updated["favorite"] is True
        assert updated["git_remote_url"] == "https://github.com/user/u.git"
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_update_project_partial(self):
        """Partial update should preserve other fields."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "p2", "name": "Partial", "path": "/tmp/p",
                               "default_backend": "claude-code", "tags": ["go"]})
        updated = await db.update_project("p2", {"name": "PartialNew"})
        assert updated["name"] == "PartialNew"
        assert updated["default_backend"] == "claude-code"
        assert updated["tags"] == ["go"]
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_update_nonexistent_project(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        result = await db.update_project("nope", {"name": "X"})
        assert result is None
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_delete_project(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "d1", "name": "Del", "path": "/tmp/d"})
        assert await db.delete_project("d1") is True
        assert await db.get_project("d1") is None
        assert await db.delete_project("d1") is False
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_get_projects_sorted_by_name(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "p1", "name": "Zebra", "path": "/tmp/z"})
        await db.save_project({"id": "p2", "name": "Alpha", "path": "/tmp/a"})
        projects = await db.get_projects()
        assert projects[0]["name"] == "Alpha"
        assert projects[1]["name"] == "Zebra"
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_save_project_with_auto_approve_patterns(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        patterns = [{"pattern": "proceed", "response": "yes"}]
        await db.save_project({"id": "ap1", "name": "AP", "path": "/tmp/ap",
                               "auto_approve_patterns": patterns})
        p = await db.get_project("ap1")
        assert p["auto_approve_patterns"] == patterns
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_json_parse_handles_corrupt_data(self):
        """Corrupt JSON in DB should fall back to empty list, not crash."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "bad", "name": "Bad", "path": "/tmp/bad"})
        # Manually corrupt the JSON columns
        await db._db.execute(
            "UPDATE projects SET tags_json = 'not-json', recent_tasks_json = '{invalid' WHERE id = 'bad'"
        )
        await db._db.commit()
        p = await db.get_project("bad")
        assert p is not None
        assert p["tags"] == []
        assert p["recent_tasks"] == []
        await db.close()
        db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Database: Recent Tasks
# ─────────────────────────────────────────────

class TestRecentTasks:
    @pytest.mark.asyncio
    async def test_add_recent_task(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "rt1", "name": "RT", "path": "/tmp/rt"})
        await db.add_recent_task("rt1", "Fix auth bug", "backend", "claude-code")
        p = await db.get_project("rt1")
        assert len(p["recent_tasks"]) == 1
        assert p["recent_tasks"][0]["task"] == "Fix auth bug"
        assert p["recent_tasks"][0]["role"] == "backend"
        assert p["recent_tasks"][0]["backend"] == "claude-code"
        assert "timestamp" in p["recent_tasks"][0]
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_recent_tasks_fifo_cap(self):
        """Recent tasks should be capped at 10, newest first."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "cap", "name": "Cap", "path": "/tmp/cap"})
        for i in range(15):
            await db.add_recent_task("cap", f"Task {i}", "backend")
        p = await db.get_project("cap")
        assert len(p["recent_tasks"]) == 10
        assert p["recent_tasks"][0]["task"] == "Task 14"  # Most recent first
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_recent_tasks_deduplication(self):
        """Adding same task again should move it to front, not duplicate."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.save_project({"id": "dd", "name": "Dedup", "path": "/tmp/dd"})
        await db.add_recent_task("dd", "Fix bug", "backend")
        await db.add_recent_task("dd", "Add tests", "tester")
        await db.add_recent_task("dd", "Fix bug", "frontend")  # Same task, different role
        p = await db.get_project("dd")
        assert len(p["recent_tasks"]) == 2
        assert p["recent_tasks"][0]["task"] == "Fix bug"
        assert p["recent_tasks"][0]["role"] == "frontend"  # Updated role
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_add_recent_task_nonexistent_project(self):
        """Should not crash on nonexistent project."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.add_recent_task("nonexistent", "Task", "backend")
        # Should not raise
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_recent_task_truncates_long_tasks(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "tr", "name": "Trunc", "path": "/tmp/tr"})
        long_task = "x" * 1000
        await db.add_recent_task("tr", long_task, "backend")
        p = await db.get_project("tr")
        assert len(p["recent_tasks"][0]["task"]) == 500
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_add_recent_task_db_not_init(self):
        """Should not crash if DB not initialized."""
        db = Database(Path("/tmp/noinit.db"))
        await db.add_recent_task("p1", "task", "role")
        # Should not raise


# ─────────────────────────────────────────────
# API: Create Project (git auto-detection)
# ─────────────────────────────────────────────

class TestCreateProjectAPI:
    @pytest.mark.asyncio
    async def test_create_project_basic(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "My Project",
            "path": TEST_WORKING_DIR,
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["name"] == "My Project"
        assert "id" in data
        # git fields may or may not be populated depending on whether path is a git repo
        assert "git_remote_url" in data

    @pytest.mark.asyncio
    async def test_create_project_with_defaults(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Configured",
            "path": TEST_WORKING_DIR,
            "default_backend": "codex",
            "default_model": "sonnet",
            "default_role": "tester",
            "tags": ["python"],
            "favorite": True,
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["default_backend"] == "codex"
        assert data["default_model"] == "sonnet"
        assert data["default_role"] == "tester"
        assert data["tags"] == ["python"]
        assert data["favorite"] is True

    @pytest.mark.asyncio
    async def test_create_project_missing_name(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={"path": TEST_WORKING_DIR})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_invalid_path(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Bad", "path": "/nonexistent/dir/xyz"
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_git_detection(self, aiohttp_client):
        """When path is a git repo, git metadata should be auto-detected."""
        # Use the Ashlr AO directory itself which is a git repo
        repo_path = str(Path(__file__).parent.parent)
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Ashlr Self", "path": repo_path,
        })
        assert resp.status == 201
        data = await resp.json()
        # This repo should have a remote and branch
        assert data.get("git_remote_url") or data.get("default_branch")

    @pytest.mark.asyncio
    async def test_create_project_non_git_dir(self, aiohttp_client):
        """Non-git directories should still succeed with empty git fields."""
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as tmpdir:
            app = _make_test_app()
            client = await aiohttp_client(app)
            resp = await client.post("/api/projects", json={
                "name": "NonGit", "path": tmpdir,
            })
            assert resp.status == 201
            data = await resp.json()
            assert data["git_remote_url"] == ""
            assert data["default_branch"] == ""


# ─────────────────────────────────────────────
# API: Update Project (new fields)
# ─────────────────────────────────────────────

class TestUpdateProjectAPI:
    @pytest.mark.asyncio
    async def test_update_project_defaults(self, aiohttp_client):
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "p1", "name": "Old", "path": "/tmp/old", "description": "",
            "git_remote_url": "", "default_branch": "", "default_backend": "",
            "default_model": "", "default_role": "", "tags": [], "favorite": False,
            "recent_tasks": [], "auto_approve_patterns": [],
        })
        app["db"].update_project = AsyncMock(return_value={
            "id": "p1", "name": "Old", "path": "/tmp/old", "description": "",
            "default_backend": "codex", "default_model": "sonnet",
            "default_role": "tester", "tags": ["rust"], "favorite": True,
            "git_remote_url": "", "default_branch": "",
            "recent_tasks": [], "auto_approve_patterns": [],
        })
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/p1", json={
            "default_backend": "codex",
            "default_model": "sonnet",
            "default_role": "tester",
            "tags": ["rust"],
            "favorite": True,
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["default_backend"] == "codex"
        assert data["favorite"] is True

    @pytest.mark.asyncio
    async def test_update_project_invalid_role(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/p1", json={
            "default_role": "nonexistent_role",
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_project_invalid_tags(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/p1", json={
            "tags": "not-a-list",
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_project_tags_non_strings(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/p1", json={
            "tags": [123, True],
        })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_project_not_found(self, aiohttp_client):
        app = _make_test_app()
        app["db"].update_project = AsyncMock(return_value=None)
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/nonexistent", json={"name": "X"})
        assert resp.status == 404


# ─────────────────────────────────────────────
# API: Project Context Endpoint
# ─────────────────────────────────────────────

class TestProjectContextAPI:
    @pytest.mark.asyncio
    async def test_get_project_context(self, aiohttp_client):
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "ctx1", "name": "CtxProject", "path": "/tmp/ctx",
            "git_remote_url": "https://github.com/user/ctx.git",
            "default_branch": "main", "default_backend": "claude-code",
            "default_model": "", "default_role": "", "tags": [],
            "favorite": False, "description": "",
            "recent_tasks": [{"task": "Fix bug", "role": "backend", "timestamp": "2026-01-01"}],
            "auto_approve_patterns": [],
        })
        client = await aiohttp_client(app)
        resp = await client.get("/api/projects/ctx1/context")
        assert resp.status == 200
        data = await resp.json()
        assert data["project"]["name"] == "CtxProject"
        assert data["recent_tasks"] == [{"task": "Fix bug", "role": "backend", "timestamp": "2026-01-01"}]
        assert "agents" in data
        assert "stats" in data
        assert data["stats"]["agent_count"] == 0

    @pytest.mark.asyncio
    async def test_get_project_context_with_agents(self, aiohttp_client, make_agent):
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "ctx2", "name": "WithAgents", "path": "/tmp/wa",
            "git_remote_url": "", "default_branch": "", "default_backend": "",
            "default_model": "", "default_role": "", "tags": [],
            "favorite": False, "description": "",
            "recent_tasks": [], "auto_approve_patterns": [],
        })
        # Add an agent linked to this project
        agent = make_agent(agent_id="ag1", project_id="ctx2", status="working")
        agent.estimated_cost_usd = 0.5
        app["agent_manager"].agents["ag1"] = agent

        client = await aiohttp_client(app)
        resp = await client.get("/api/projects/ctx2/context")
        assert resp.status == 200
        data = await resp.json()
        assert data["stats"]["agent_count"] == 1
        assert data["stats"]["active_count"] == 1
        assert data["stats"]["total_cost"] == 0.5

    @pytest.mark.asyncio
    async def test_get_project_context_not_found(self, aiohttp_client):
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value=None)
        client = await aiohttp_client(app)
        resp = await client.get("/api/projects/nonexistent/context")
        assert resp.status == 404


# ─────────────────────────────────────────────
# API: Project-Aware Spawn
# ─────────────────────────────────────────────

class TestProjectAwareSpawn:
    @pytest.mark.asyncio
    async def test_spawn_with_project_defaults(self, aiohttp_client):
        """When project_id provided but backend/model not specified, use project defaults."""
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "sp1", "name": "SpawnProj", "path": TEST_WORKING_DIR,
            "default_backend": "claude-code", "default_model": "opus",
            "default_role": "backend", "default_branch": "main",
            "git_remote_url": "", "tags": [], "favorite": False,
            "description": "", "recent_tasks": [], "auto_approve_patterns": [],
        })
        app["db"].add_recent_task = AsyncMock()

        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "Build the auth module",
            "project_id": "sp1",
        })
        assert resp.status == 201
        data = await resp.json()
        # Agent should have project defaults applied
        assert data["project_id"] == "sp1"

    @pytest.mark.asyncio
    async def test_spawn_explicit_overrides_project_defaults(self, aiohttp_client):
        """Explicit backend/model in request should override project defaults."""
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "sp2", "name": "Override", "path": TEST_WORKING_DIR,
            "default_backend": "codex", "default_model": "opus",
            "default_role": "backend", "default_branch": "",
            "git_remote_url": "", "tags": [], "favorite": False,
            "description": "", "recent_tasks": [], "auto_approve_patterns": [],
        })
        app["db"].add_recent_task = AsyncMock()

        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "Test the app",
            "project_id": "sp2",
            "backend": "claude-code",
            "role": "tester",
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["role"] == "tester"  # Explicit, not project default

    @pytest.mark.asyncio
    async def test_spawn_records_recent_task(self, aiohttp_client):
        """Spawn should record the task in project's recent_tasks."""
        app = _make_test_app()
        app["db"].get_project = AsyncMock(return_value={
            "id": "sp3", "name": "RecTask", "path": TEST_WORKING_DIR,
            "default_backend": "", "default_model": "", "default_role": "",
            "default_branch": "", "git_remote_url": "", "tags": [],
            "favorite": False, "description": "", "recent_tasks": [],
            "auto_approve_patterns": [],
        })
        add_mock = AsyncMock()
        app["db"].add_recent_task = add_mock

        client = await aiohttp_client(app)
        await client.post("/api/agents", json={
            "task": "Build login page",
            "project_id": "sp3",
        })
        add_mock.assert_called_once()
        call_args = add_mock.call_args
        assert call_args[0][0] == "sp3"
        assert call_args[0][1] == "Build login page"

    @pytest.mark.asyncio
    async def test_spawn_auto_assigns_project(self, aiohttp_client):
        """When no project_id given, auto-assign based on working_dir match."""
        app = _make_test_app()
        app["db"].get_projects = AsyncMock(return_value=[
            {"id": "auto1", "name": "Home", "path": str(Path.home())},
        ])
        app["db"].add_recent_task = AsyncMock()

        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "Do something",
            "working_dir": TEST_WORKING_DIR,
        })
        assert resp.status == 201
        data = await resp.json()
        assert data["project_id"] == "auto1"

    @pytest.mark.asyncio
    async def test_spawn_without_project_no_crash(self, aiohttp_client):
        """Spawn without project_id and no matching projects should still work."""
        app = _make_test_app()
        app["db"].get_projects = AsyncMock(return_value=[])
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "Do work",
        })
        assert resp.status == 201


# ─────────────────────────────────────────────
# API: List Projects (enriched)
# ─────────────────────────────────────────────

class TestListProjectsAPI:
    @pytest.mark.asyncio
    async def test_list_projects_enriched(self, aiohttp_client, make_agent):
        app = _make_test_app()
        app["db"].get_projects = AsyncMock(return_value=[
            {"id": "lp1", "name": "Listed", "path": "/tmp/l",
             "git_remote_url": "https://github.com/u/r.git",
             "default_branch": "main", "tags": ["python"], "favorite": True,
             "default_backend": "", "default_model": "", "default_role": "",
             "description": "", "recent_tasks": [], "auto_approve_patterns": []},
        ])
        agent = make_agent(agent_id="la1", project_id="lp1", status="working")
        agent.estimated_cost_usd = 0.25
        app["agent_manager"].agents["la1"] = agent

        client = await aiohttp_client(app)
        resp = await client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["agent_count"] == 1
        assert data[0]["active_count"] == 1
        assert data[0]["total_cost"] == 0.25
        # New fields should be present
        assert data[0]["git_remote_url"] == "https://github.com/u/r.git"
        assert data[0]["tags"] == ["python"]

    @pytest.mark.asyncio
    async def test_list_projects_empty(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        assert data == []


# ─────────────────────────────────────────────
# Database: Edge Cases
# ─────────────────────────────────────────────

class TestProjectDatabaseEdgeCases:
    @pytest.mark.asyncio
    async def test_save_project_overwrite(self):
        """INSERT OR REPLACE should overwrite existing project."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "ow1", "name": "V1", "path": "/tmp/v1"})
        await db.save_project({"id": "ow1", "name": "V2", "path": "/tmp/v2",
                               "tags": ["new"]})
        p = await db.get_project("ow1")
        assert p["name"] == "V2"
        assert p["tags"] == ["new"]
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_favorite_stored_as_bool(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "fb1", "name": "Fav", "path": "/tmp/fav", "favorite": True})
        p = await db.get_project("fb1")
        assert p["favorite"] is True
        assert isinstance(p["favorite"], bool)
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_empty_tags_serialization(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "et1", "name": "Empty", "path": "/tmp/e"})
        p = await db.get_project("et1")
        assert p["tags"] == []
        assert isinstance(p["tags"], list)
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_update_favorite_toggle(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "ft1", "name": "Toggle", "path": "/tmp/t", "favorite": False})
        updated = await db.update_project("ft1", {"favorite": True})
        assert updated["favorite"] is True
        updated = await db.update_project("ft1", {"favorite": False})
        assert updated["favorite"] is False
        await db.close()
        db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_get_projects_no_db(self):
        """get_projects with uninitialized DB returns empty list."""
        db = Database(Path("/tmp/nodb.db"))
        result = await db.get_projects()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_project_no_db(self):
        db = Database(Path("/tmp/nodb.db"))
        result = await db.get_project("p1")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_project_no_db(self):
        db = Database(Path("/tmp/nodb.db"))
        await db.save_project({"id": "p1", "name": "X", "path": "/tmp"})
        # Should not raise

    @pytest.mark.asyncio
    async def test_update_project_no_db(self):
        db = Database(Path("/tmp/nodb.db"))
        result = await db.update_project("p1", {"name": "X"})
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_project_no_db(self):
        db = Database(Path("/tmp/nodb.db"))
        result = await db.delete_project("p1")
        assert result is False

    @pytest.mark.asyncio
    async def test_add_recent_task_no_db(self):
        db = Database(Path("/tmp/nodb.db"))
        await db.add_recent_task("p1", "task", "role")
        # Should not raise

    @pytest.mark.asyncio
    async def test_parse_project_row_empty_json(self):
        """_parse_project_row should handle empty string JSON gracefully."""
        from ashlr_ao.database import Database as DB
        mock_row = {
            "id": "x", "name": "X", "path": "/tmp",
            "tags_json": "", "recent_tasks_json": "",
            "auto_approve_patterns_json": "", "favorite": 0,
        }
        result = DB._parse_project_row(mock_row)
        assert result["tags"] == []
        assert result["recent_tasks"] == []
        assert result["auto_approve_patterns"] == []
        assert result["favorite"] is False

    @pytest.mark.asyncio
    async def test_multiple_projects_tags_independent(self):
        """Tags on one project should not affect another."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "mp1", "name": "A", "path": "/tmp/a", "tags": ["python"]})
        await db.save_project({"id": "mp2", "name": "B", "path": "/tmp/b", "tags": ["rust"]})
        p1 = await db.get_project("mp1")
        p2 = await db.get_project("mp2")
        assert p1["tags"] == ["python"]
        assert p2["tags"] == ["rust"]
        await db.close()
        db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# API: Create Project Additional Validation
# ─────────────────────────────────────────────

class TestCreateProjectValidation:
    @pytest.mark.asyncio
    async def test_create_project_path_outside_home(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", json={
            "name": "Bad", "path": "/etc/hosts"
        })
        assert resp.status == 400
        data = await resp.json()
        assert "home" in data["error"].lower() or "directory" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_create_project_invalid_json(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/projects", data=b"not json",
                                  headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_project_empty_body(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/projects/p1", json={})
        assert resp.status == 400


# ─────────────────────────────────────────────
# Wave 2: Enhanced Search Endpoint
# ─────────────────────────────────────────────

class TestEnhancedSearch:
    @pytest.mark.asyncio
    async def test_search_basic(self, aiohttp_client, make_agent):
        app = _make_test_app()
        agent = make_agent(agent_id="s1", name="searcher")
        agent.output_lines.append("Hello world this is a test")
        agent.output_lines.append("Another line with error message")
        app["agent_manager"].agents["s1"] = agent
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=error")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["agent_id"] == "s1"

    @pytest.mark.asyncio
    async def test_search_filter_by_project(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a1 = make_agent(agent_id="sp1", name="proj-agent", project_id="proj1")
        a1.output_lines.append("found match here")
        a2 = make_agent(agent_id="sp2", name="other-agent", project_id="proj2")
        a2.output_lines.append("found match here too")
        app["agent_manager"].agents["sp1"] = a1
        app["agent_manager"].agents["sp2"] = a2
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=found&project_id=proj1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["agent_id"] == "sp1"

    @pytest.mark.asyncio
    async def test_search_filter_by_status(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a1 = make_agent(agent_id="st1", name="working-agent", status="working")
        a1.output_lines.append("match line")
        a2 = make_agent(agent_id="st2", name="idle-agent", status="idle")
        a2.output_lines.append("match line")
        app["agent_manager"].agents["st1"] = a1
        app["agent_manager"].agents["st2"] = a2
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=match&status=working")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["status"] == "working"

    @pytest.mark.asyncio
    async def test_search_combined_filters(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a1 = make_agent(agent_id="cf1", project_id="p1", status="working")
        a1.output_lines.append("combined match")
        a2 = make_agent(agent_id="cf2", project_id="p1", status="idle")
        a2.output_lines.append("combined match")
        a3 = make_agent(agent_id="cf3", project_id="p2", status="working")
        a3.output_lines.append("combined match")
        for a in [a1, a2, a3]:
            app["agent_manager"].agents[a.id] = a
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=combined&project_id=p1&status=working")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["agent_id"] == "cf1"

    @pytest.mark.asyncio
    async def test_search_includes_project_id(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a = make_agent(agent_id="pi1", project_id="myproj")
        a.output_lines.append("test output")
        app["agent_manager"].agents["pi1"] = a
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=test")
        assert resp.status == 200
        data = await resp.json()
        assert data["results"][0]["project_id"] == "myproj"

    @pytest.mark.asyncio
    async def test_search_short_query(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=x")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_search_too_long_query(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get(f"/api/search?q={'x' * 201}")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_search_empty_results(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a = make_agent(agent_id="ne1")
        a.output_lines.append("nothing relevant")
        app["agent_manager"].agents["ne1"] = a
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=zzzznotfound")
        assert resp.status == 200
        data = await resp.json()
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_search_custom_limit(self, aiohttp_client, make_agent):
        app = _make_test_app()
        a = make_agent(agent_id="lm1")
        for i in range(30):
            a.output_lines.append(f"line {i} with keyword")
        app["agent_manager"].agents["lm1"] = a
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=keyword&limit=5")
        assert resp.status == 200
        data = await resp.json()
        assert data["results"][0]["match_count"] <= 5

    @pytest.mark.asyncio
    async def test_search_agents_searched_count(self, aiohttp_client, make_agent):
        """agents_searched should count only agents that pass filters."""
        app = _make_test_app()
        for i in range(3):
            a = make_agent(agent_id=f"asc{i}", project_id=f"p{i % 2}")
            a.output_lines.append("data")
            app["agent_manager"].agents[a.id] = a
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=data&project_id=p0")
        data = await resp.json()
        assert data["agents_searched"] == 2  # p0 has asc0 and asc2

    @pytest.mark.asyncio
    async def test_search_no_agents(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/search?q=test")
        assert resp.status == 200
        data = await resp.json()
        assert data["results"] == []
        assert data["agents_searched"] == 0

    @pytest.mark.asyncio
    async def test_search_missing_query(self, aiohttp_client):
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/search")
        assert resp.status == 400


# ═══════════════════════════════════════════════
# Audit fix: save_project preserves org_id
# ═══════════════════════════════════════════════


@pytest.mark.asyncio
class TestSaveProjectOrgId:
    """Audit: save_project INSERT OR REPLACE must preserve org_id."""

    async def test_save_project_preserves_org_id(self):
        """org_id should survive save_project INSERT OR REPLACE."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({
            "id": "org1", "name": "OrgTest", "path": "/tmp/org",
            "org_id": "myorg123",
        })
        p = await db.get_project("org1")
        assert p is not None
        assert p.get("org_id") == "myorg123"

        # Save again (INSERT OR REPLACE) — org_id must survive
        await db.save_project({
            "id": "org1", "name": "OrgTest Updated", "path": "/tmp/org",
            "org_id": "myorg123",
        })
        p2 = await db.get_project("org1")
        assert p2 is not None
        assert p2.get("org_id") == "myorg123"
        assert p2["name"] == "OrgTest Updated"
        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_save_project_without_org_id_defaults_empty(self):
        """save_project without org_id should default to empty string."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        await db.save_project({"id": "no_org", "name": "NoOrg", "path": "/tmp/no"})
        p = await db.get_project("no_org")
        assert p is not None
        assert p.get("org_id", "") == ""
        await db.close()
        db_path.unlink(missing_ok=True)
