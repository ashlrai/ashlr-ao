"""Shared test fixtures for Ashlr AO."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import field
from unittest.mock import AsyncMock, MagicMock, patch
import collections
import time

import pytest

# Add project root to path so we can import ashlr_server
sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch psutil.cpu_percent before importing ashlr_server (it runs at import time)
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server

# Use home directory for working_dir in tests (server validates against home)
TEST_WORKING_DIR = str(Path.home())


# ── Shared helper functions (usable directly or via fixtures) ──

def make_mock_db():
    """Create a comprehensive mock Database that returns empty results without needing SQLite."""
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
    db.get_messages_for_agent = AsyncMock(return_value=[])
    db.get_messages_between = AsyncMock(return_value=[])
    db.get_message_count_for_agent = AsyncMock(return_value=0)
    db.mark_messages_read = AsyncMock(return_value=0)
    db.get_unread_count = AsyncMock(return_value=0)
    db.upsert_scratchpad = AsyncMock()
    db.delete_scratchpad = AsyncMock(return_value=False)
    db.save_bookmark = AsyncMock(return_value=1)
    db.get_history_item = AsyncMock(return_value=None)
    db.get_workflow = AsyncMock(return_value=None)
    db.get_agent_history_item = AsyncMock(return_value=None)
    db.get_project = AsyncMock(return_value=None)
    db.update_project = AsyncMock(return_value=None)
    db.add_recent_task = AsyncMock()
    db.get_preset = AsyncMock(return_value=None)
    db.get_agent_history = AsyncMock(return_value=[])
    db.update_org_license = AsyncMock()
    db.get_org_license_key = AsyncMock(return_value="")
    db.cleanup_old_archives = AsyncMock(return_value=0)
    # Fleet templates
    db.get_fleet_templates = AsyncMock(return_value=[])
    db.get_fleet_template = AsyncMock(return_value=None)
    db.save_fleet_template = AsyncMock()
    db.delete_fleet_template = AsyncMock(return_value=False)
    db._db = None
    return db


def make_test_app(license=None):
    """Create a test app with DB and background tasks disabled.

    Args:
        license: Optional License object. Defaults to Pro license for full feature access.
    """
    config = ashlr_server.Config()
    config.demo_mode = True
    config.spawn_pressure_block = False

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
    # Set Pro license so tests bypass feature gates (unless overridden)
    if license is None:
        from ashlr_server import License, PRO_FEATURES
        license = License(
            tier="pro", max_agents=100,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
            features=PRO_FEATURES,
        )
    app["license"] = license
    app["agent_manager"].license = license
    return app


# ── Pytest fixtures ──

@pytest.fixture
def mock_db():
    """Fixture returning a mock Database."""
    return make_mock_db()


@pytest.fixture
def test_app():
    """Fixture returning a fully configured test app."""
    return make_test_app()


@pytest.fixture
def make_agent():
    """Factory fixture to create Agent instances with sensible defaults."""
    def _make(
        agent_id="a1b2",
        name="test-agent",
        role="general",
        status="working",
        task="Test task",
        backend="claude-code",
        working_dir="/tmp/test",
        error_count=0,
        memory_mb=0.0,
        **kwargs,
    ):
        agent = ashlr_server.Agent(
            id=agent_id,
            name=name,
            role=role,
            status=status,
            task=task,
            backend=backend,
            working_dir=working_dir,
            tmux_session=f"ashlr-{agent_id}",
        )
        agent.error_count = error_count
        agent.memory_mb = memory_mb
        agent._spawn_time = time.monotonic() - 60  # spawned 60s ago
        agent.last_output_time = time.monotonic() - 5  # output 5s ago
        agent.created_at = ashlr_server.datetime.now(
            ashlr_server.timezone.utc
        ).isoformat()
        for k, v in kwargs.items():
            setattr(agent, k, v)
        return agent
    return _make


@pytest.fixture
def config():
    """Create a default Config instance."""
    return ashlr_server.Config()
