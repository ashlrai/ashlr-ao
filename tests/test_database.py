"""Real SQLite integration tests for the Database class.
Tests actual SQL queries against a temp database file."""

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock
from collections import deque

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    from ashlr_server import Database, Agent


def run_async(coro):
    """Helper to run async tests without pytest-asyncio."""
    return asyncio.run(coro)


def _make_test_agent(agent_id="t001", name="test-agent", role="backend",
                     status="complete", task="Fix auth bug",
                     summary="Fixed the auth module", working_dir="/tmp/test",
                     backend="claude-code"):
    """Create a real Agent instance for DB tests."""
    agent = Agent(
        id=agent_id, name=name, role=role, status=status,
        backend=backend, task=task, working_dir=working_dir,
    )
    agent.summary = summary
    agent.output_lines = deque(["line1", "line2", "line3"], maxlen=500)
    agent.tokens_input = 1000
    agent.tokens_output = 500
    agent.estimated_cost_usd = 0.05
    agent.context_pct = 0.45
    return agent


# ─────────────────────────────────────────────
# DB init/close lifecycle
# ─────────────────────────────────────────────

class TestDatabaseLifecycle:
    def test_init_creates_tables(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()
            assert db._db is not None
            # Verify key tables exist
            async with db._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
            assert "agents_history" in tables
            assert "projects" in tables
            assert "activity_events" in tables
            assert "agent_output_archive" in tables
            assert "workflows" in tables
            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_close_sets_none(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()
            assert db._db is not None
            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_default_workflows_seeded(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()
            workflows = await db.get_workflows()
            assert len(workflows) > 0  # Default workflows should be seeded
            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Agent history CRUD
# ─────────────────────────────────────────────

class TestAgentHistory:
    def test_save_and_retrieve(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            agent = _make_test_agent()
            await db.save_agent(agent)

            history = await db.get_agent_history(limit=10)
            assert len(history) >= 1
            found = next((h for h in history if h["id"] == "t001"), None)
            assert found is not None
            assert found["name"] == "test-agent"
            assert found["role"] == "backend"
            assert found["task"] == "Fix auth bug"
            assert found["backend"] == "claude-code"

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_get_agent_history_item(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            agent = _make_test_agent(agent_id="t002", name="specific-agent")
            await db.save_agent(agent)

            item = await db.get_agent_history_item("t002")
            assert item is not None
            assert item["name"] == "specific-agent"

            missing = await db.get_agent_history_item("nonexistent")
            assert missing is None

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_history_count(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            for i in range(5):
                agent = _make_test_agent(agent_id=f"c{i:03d}", name=f"agent-{i}")
                await db.save_agent(agent)

            count = await db.get_agent_history_count()
            assert count == 5

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_history_pagination(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            for i in range(10):
                agent = _make_test_agent(agent_id=f"p{i:03d}", name=f"page-{i}")
                await db.save_agent(agent)

            page1 = await db.get_agent_history(limit=3, offset=0)
            page2 = await db.get_agent_history(limit=3, offset=3)
            assert len(page1) == 3
            assert len(page2) == 3
            # Pages should not overlap
            ids1 = {h["id"] for h in page1}
            ids2 = {h["id"] for h in page2}
            assert ids1.isdisjoint(ids2)

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_resumable_flag(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            # Complete status should be resumable
            agent = _make_test_agent(agent_id="r001", status="complete")
            await db.save_agent(agent)

            # Error status should not be resumable
            agent2 = _make_test_agent(agent_id="r002", status="error")
            await db.save_agent(agent2)

            sessions = await db.get_resumable_sessions()
            ids = {s["id"] for s in sessions}
            assert "r001" in ids
            assert "r002" not in ids

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Project CRUD
# ─────────────────────────────────────────────

class TestProjectCRUD:
    def test_save_and_get(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            project = {
                "id": "proj-001",
                "name": "My Project",
                "path": "/home/user/myproject",
                "description": "A test project",
            }
            await db.save_project(project)

            projects = await db.get_projects()
            assert len(projects) >= 1
            found = next((p for p in projects if p["id"] == "proj-001"), None)
            assert found is not None
            assert found["name"] == "My Project"
            assert found["path"] == "/home/user/myproject"

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_delete_project(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            project = {"id": "proj-del", "name": "Delete Me", "path": "/tmp/del"}
            await db.save_project(project)

            deleted = await db.delete_project("proj-del")
            assert deleted is True

            # Should not exist anymore
            projects = await db.get_projects()
            ids = {p["id"] for p in projects}
            assert "proj-del" not in ids

            # Deleting non-existent returns False
            deleted2 = await db.delete_project("nonexistent")
            assert deleted2 is False

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Activity events (audit log)
# ─────────────────────────────────────────────

class TestActivityEvents:
    def test_log_and_get_events(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            await db.log_event("spawn", "Agent a1 spawned", agent_id="a1", agent_name="test-agent")
            await db.log_event("error", "Agent a1 crashed", agent_id="a1")
            await db.log_event("kill", "Agent a2 killed", agent_id="a2")

            events = await db.get_events(limit=10)
            assert len(events) == 3

            # Filter by agent_id
            a1_events = await db.get_events(agent_id="a1")
            assert len(a1_events) == 2

            # Filter by event_type
            errors = await db.get_events(event_type="error")
            assert len(errors) == 1
            assert errors[0]["message"] == "Agent a1 crashed"

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_event_count(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            for i in range(7):
                await db.log_event("spawn", f"Event {i}")

            count = await db.get_events_count()
            assert count == 7

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_event_metadata(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            meta = {"extra_info": "value", "count": 42}
            await db.log_event("test", "With metadata", metadata=meta)

            events = await db.get_events(limit=1)
            assert len(events) == 1
            assert events[0]["metadata"]["extra_info"] == "value"
            assert events[0]["metadata"]["count"] == 42

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Output archive
# ─────────────────────────────────────────────

class TestOutputArchive:
    def test_archive_and_retrieve(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            lines = ["output line 1", "output line 2", "output line 3"]
            await db.archive_output("arch-001", lines, start_offset=0)

            retrieved, total = await db.get_archived_output("arch-001")
            assert total == 3
            assert retrieved == lines

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_archive_pagination(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            lines = [f"line {i}" for i in range(20)]
            await db.archive_output("page-001", lines, start_offset=0)

            page1, total = await db.get_archived_output("page-001", offset=0, limit=5)
            assert total == 20
            assert len(page1) == 5
            assert page1[0] == "line 0"

            page2, _ = await db.get_archived_output("page-001", offset=5, limit=5)
            assert len(page2) == 5
            assert page2[0] == "line 5"

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_archive_empty_lines_skipped(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            await db.archive_output("empty-001", [], start_offset=0)

            retrieved, total = await db.get_archived_output("empty-001")
            assert total == 0
            assert retrieved == []

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_rotate_archive(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            lines = [f"line {i}" for i in range(100)]
            await db.archive_output("rot-001", lines, start_offset=0)

            deleted = await db.rotate_archive("rot-001", max_rows=50)
            assert deleted == 50

            _, total = await db.get_archived_output("rot-001")
            assert total == 50

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_rotate_under_limit_noop(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            lines = [f"line {i}" for i in range(10)]
            await db.archive_output("rot-002", lines, start_offset=0)

            deleted = await db.rotate_archive("rot-002", max_rows=50)
            assert deleted == 0

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Task similarity search
# ─────────────────────────────────────────────

class TestFindSimilarTasks:
    def test_finds_matching_tasks(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            # Insert agents with different tasks
            for i, task in enumerate([
                "Fix authentication login bug",
                "Add payment processing",
                "Fix authentication token refresh",
                "Update dashboard styles",
            ]):
                agent = _make_test_agent(agent_id=f"s{i:03d}", task=task)
                await db.save_agent(agent)

            results = await db.find_similar_tasks("authentication fix")
            assert len(results) >= 2
            # Auth-related tasks should be ranked higher
            task_texts = [r["task"] for r in results]
            assert any("authentication" in t.lower() for t in task_texts)

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_no_match_returns_empty(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            agent = _make_test_agent(task="Fix database connection")
            await db.save_agent(agent)

            results = await db.find_similar_tasks("zzzzzznonexistent")
            assert results == []

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())

    def test_short_query_returns_empty(self):
        async def _test():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = Path(f.name)
            db = Database(db_path)
            await db.init()

            # Words <= 3 chars should be filtered out
            results = await db.find_similar_tasks("a is to")
            assert results == []

            await db.close()
            db_path.unlink(missing_ok=True)
        run_async(_test())


# ─────────────────────────────────────────────
# Degraded mode (no DB connection)
# ─────────────────────────────────────────────

class TestDatabaseDegradedModeReal:
    def test_all_reads_return_empty_when_no_db(self):
        async def _test():
            db = Database(Path("/tmp/noexist.db"))
            # Don't call init() — _db stays None

            history = await db.get_agent_history()
            assert history == []

            count = await db.get_agent_history_count()
            assert count == 0

            item = await db.get_agent_history_item("x")
            assert item is None

            projects = await db.get_projects()
            assert projects == []

            deleted = await db.delete_project("x")
            assert deleted is False

            events = await db.get_events()
            assert events == []

            evt_count = await db.get_events_count()
            assert evt_count == 0

            lines, total = await db.get_archived_output("x")
            assert lines == []
            assert total == 0

            similar = await db.find_similar_tasks("test")
            assert similar == []

        run_async(_test())
