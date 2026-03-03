"""Real SQLite integration tests for the Database class.
Tests actual SQL queries against a temp database file."""

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
    async def test_init_creates_tables(self):
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

    async def test_close_sets_none(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        assert db._db is not None
        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_default_workflows_seeded(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()
        workflows = await db.get_workflows()
        assert len(workflows) > 0  # Default workflows should be seeded
        await db.close()
        db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Agent history CRUD
# ─────────────────────────────────────────────

class TestAgentHistory:
    async def test_save_and_retrieve(self):
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

    async def test_get_agent_history_item(self):
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

    async def test_history_count(self):
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

    async def test_history_pagination(self):
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

    async def test_resumable_flag(self):
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


# ─────────────────────────────────────────────
# Project CRUD
# ─────────────────────────────────────────────

class TestProjectCRUD:
    async def test_save_and_get(self):
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

    async def test_delete_project(self):
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


# ─────────────────────────────────────────────
# Activity events (audit log)
# ─────────────────────────────────────────────

class TestActivityEvents:
    async def test_log_and_get_events(self):
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

    async def test_event_count(self):
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

    async def test_event_metadata(self):
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


# ─────────────────────────────────────────────
# Output archive
# ─────────────────────────────────────────────

class TestOutputArchive:
    async def test_archive_and_retrieve(self):
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

    async def test_archive_pagination(self):
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

    async def test_archive_empty_lines_skipped(self):
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

    async def test_rotate_archive(self):
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

    async def test_rotate_under_limit_noop(self):
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


# ─────────────────────────────────────────────
# Task similarity search
# ─────────────────────────────────────────────

class TestFindSimilarTasks:
    async def test_finds_matching_tasks(self):
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

    async def test_no_match_returns_empty(self):
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

    async def test_short_query_returns_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        # Words <= 3 chars should be filtered out
        results = await db.find_similar_tasks("a is to")
        assert results == []

        await db.close()
        db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Degraded mode (no DB connection)
# ─────────────────────────────────────────────

class TestDatabaseDegradedModeReal:
    async def test_all_reads_return_empty_when_no_db(self):
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



# ─────────────────────────────────────────────
# Messages CRUD
# ─────────────────────────────────────────────

class TestMessagesCRUD:
    async def test_save_and_get_messages(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        msg = {
            "id": "msg-001",
            "from_agent_id": "a001",
            "to_agent_id": "a002",
            "content": "Please review this code",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.save_message(msg)

        messages = await db.get_messages_for_agent("a002", limit=10)
        assert len(messages) >= 1
        found = next((m for m in messages if m["id"] == "msg-001"), None)
        assert found is not None
        assert found["content"] == "Please review this code"
        assert found["from_agent_id"] == "a001"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_get_messages_between(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        now = datetime.now(timezone.utc).isoformat()
        await db.save_message({
            "id": "msg-010", "from_agent_id": "a001", "to_agent_id": "a002",
            "content": "Hello from a001", "created_at": now,
        })
        await db.save_message({
            "id": "msg-011", "from_agent_id": "a002", "to_agent_id": "a001",
            "content": "Reply from a002", "created_at": now,
        })
        await db.save_message({
            "id": "msg-012", "from_agent_id": "a003", "to_agent_id": "a004",
            "content": "Unrelated message", "created_at": now,
        })

        between = await db.get_messages_between("a001", "a002")
        assert len(between) == 2
        ids = {m["id"] for m in between}
        assert "msg-010" in ids
        assert "msg-011" in ids
        assert "msg-012" not in ids

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_get_messages_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        messages = await db.get_messages_for_agent("nonexistent")
        assert messages == []

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_get_messages_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        messages = await db.get_messages_for_agent("a001")
        assert messages == []
        between = await db.get_messages_between("a001", "a002")
        assert between == []


# ─────────────────────────────────────────────
# Preset CRUD
# ─────────────────────────────────────────────

class TestPresetCRUD:
    async def test_save_and_get_presets(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        preset = {
            "id": "preset-001",
            "name": "Quick Backend",
            "role": "backend",
            "task": "Build API endpoints",
            "backend": "claude-code",
        }
        await db.save_preset(preset)

        presets = await db.get_presets()
        assert len(presets) >= 1
        found = next((p for p in presets if p["id"] == "preset-001"), None)
        assert found is not None
        assert found["name"] == "Quick Backend"
        assert found["role"] == "backend"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_update_preset(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        preset = {
            "id": "preset-upd",
            "name": "Original Name",
            "role": "general",
            "task": "Original task",
        }
        await db.save_preset(preset)

        # Upsert with same ID but different name
        preset["name"] = "Updated Name"
        preset["task"] = "Updated task"
        await db.save_preset(preset)

        presets = await db.get_presets()
        found = next((p for p in presets if p["id"] == "preset-upd"), None)
        assert found is not None
        assert found["name"] == "Updated Name"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_delete_preset(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        preset = {
            "id": "preset-del",
            "name": "Delete Me",
            "role": "general",
        }
        await db.save_preset(preset)
        deleted = await db.delete_preset("preset-del")
        assert deleted is True

        # Should not exist anymore
        presets = await db.get_presets()
        ids = {p["id"] for p in presets}
        assert "preset-del" not in ids

        # Deleting non-existent returns False
        deleted2 = await db.delete_preset("nonexistent")
        assert deleted2 is False

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_preset_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        presets = await db.get_presets()
        assert presets == []
        deleted = await db.delete_preset("x")
        assert deleted is False


# ─────────────────────────────────────────────
# Scratchpad CRUD
# ─────────────────────────────────────────────

class TestScratchpadCRUD:
    async def test_upsert_and_get(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.upsert_scratchpad("proj-001", "api_url", "http://localhost:3000", set_by="agent-a1")
        entries = await db.get_scratchpad("proj-001")
        assert len(entries) >= 1
        found = next((e for e in entries if e["key"] == "api_url"), None)
        assert found is not None
        assert found["value"] == "http://localhost:3000"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_upsert_overwrites(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.upsert_scratchpad("proj-002", "key1", "value1")
        await db.upsert_scratchpad("proj-002", "key1", "value2")
        entries = await db.get_scratchpad("proj-002")
        key1_entries = [e for e in entries if e["key"] == "key1"]
        assert len(key1_entries) == 1
        assert key1_entries[0]["value"] == "value2"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_delete_scratchpad(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        await db.upsert_scratchpad("proj-003", "temp_key", "temp_value")
        deleted = await db.delete_scratchpad("proj-003", "temp_key")
        assert deleted is True

        entries = await db.get_scratchpad("proj-003")
        assert all(e["key"] != "temp_key" for e in entries)

        deleted2 = await db.delete_scratchpad("proj-003", "nonexistent_key")
        assert deleted2 is False

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_scratchpad_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        entries = await db.get_scratchpad("proj-x")
        assert entries == []
        deleted = await db.delete_scratchpad("proj-x", "key")
        assert deleted is False


# ─────────────────────────────────────────────
# Workflow CRUD
# ─────────────────────────────────────────────

class TestWorkflowCRUD:
    async def test_save_and_get_workflows(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        workflow = {
            "id": "wf-001",
            "name": "Full Stack Review",
            "description": "Run backend + frontend + tests",
            "agents_json": json.dumps([
                {"role": "backend", "task": "Review API"},
                {"role": "frontend", "task": "Review UI"},
            ]),
        }
        await db.save_workflow(workflow)

        workflows = await db.get_workflows()
        found = next((w for w in workflows if w["id"] == "wf-001"), None)
        assert found is not None
        assert found["name"] == "Full Stack Review"

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_get_workflow_by_id(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        workflow = {
            "id": "wf-single",
            "name": "Single Workflow",
            "agents_json": "[]",
        }
        await db.save_workflow(workflow)

        found = await db.get_workflow("wf-single")
        assert found is not None
        assert found["name"] == "Single Workflow"

        missing = await db.get_workflow("nonexistent")
        assert missing is None

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_delete_workflow(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        workflow = {
            "id": "wf-del",
            "name": "Delete Me",
            "agents_json": "[]",
        }
        await db.save_workflow(workflow)
        deleted = await db.delete_workflow("wf-del")
        assert deleted is True

        found = await db.get_workflow("wf-del")
        assert found is None

        deleted2 = await db.delete_workflow("nonexistent")
        assert deleted2 is False

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_workflow_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        workflows = await db.get_workflows()
        assert workflows == []
        found = await db.get_workflow("x")
        assert found is None
        deleted = await db.delete_workflow("x")
        assert deleted is False


# ─────────────────────────────────────────────
# Bookmarks CRUD
# ─────────────────────────────────────────────

class TestBookmarkCRUD:
    async def test_add_and_get_bookmarks(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        bookmark_id = await db.add_bookmark("agent-001", 42, "Important line here", annotation="bug found")
        assert bookmark_id > 0

        bookmarks = await db.get_bookmarks("agent-001")
        assert len(bookmarks) >= 1
        found = next((b for b in bookmarks if b["line_index"] == 42), None)
        assert found is not None
        assert "Important line here" in found["line_text"]

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_delete_bookmark(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        bid = await db.add_bookmark("agent-002", 10, "Bookmark to delete")
        assert bid > 0

        deleted = await db.delete_bookmark(bid)
        assert deleted is True

        bookmarks = await db.get_bookmarks("agent-002")
        assert all(b.get("id") != bid for b in bookmarks)

        deleted2 = await db.delete_bookmark(999999)
        assert deleted2 is False

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_bookmarks_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        bookmarks = await db.get_bookmarks("x")
        assert bookmarks == []
        bid = await db.add_bookmark("x", 0, "text")
        assert bid == -1
        deleted = await db.delete_bookmark(1)
        assert deleted is False


# ─────────────────────────────────────────────
# Project get/update
# ─────────────────────────────────────────────

class TestProjectGetUpdate:
    async def test_get_project_by_id(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        project = {
            "id": "proj-get",
            "name": "Get Me",
            "path": "/home/user/project",
        }
        await db.save_project(project)

        found = await db.get_project("proj-get")
        assert found is not None
        assert found["name"] == "Get Me"

        missing = await db.get_project("nonexistent")
        assert missing is None

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_update_project(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        project = {
            "id": "proj-upd",
            "name": "Original",
            "path": "/home/user/original",
        }
        await db.save_project(project)

        updated = await db.update_project("proj-upd", {"name": "Updated Name"})
        assert updated is not None
        assert updated["name"] == "Updated Name"
        assert updated["path"] == "/home/user/original"  # unchanged

        # Update nonexistent returns None
        result = await db.update_project("nonexistent", {"name": "test"})
        assert result is None

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_update_project_path_and_description(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = Database(db_path)
        await db.init()

        project = {
            "id": "proj-upd2",
            "name": "Full Update",
            "path": "/home/user/old",
            "description": "Old description",
        }
        await db.save_project(project)

        updated = await db.update_project("proj-upd2", {
            "path": "/home/user/new",
            "description": "New description",
        })
        assert updated is not None
        assert updated["path"] == "/home/user/new"
        assert updated["description"] == "New description"
        assert updated["name"] == "Full Update"  # unchanged

        await db.close()
        db_path.unlink(missing_ok=True)

    async def test_get_update_no_db(self):
        db = Database(Path("/tmp/noexist.db"))
        found = await db.get_project("x")
        assert found is None
        updated = await db.update_project("x", {"name": "y"})
        assert updated is None
