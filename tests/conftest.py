"""Shared test fixtures for Ashlr AO."""

import sys
from pathlib import Path
from dataclasses import field
from unittest.mock import patch
import collections
import time

import pytest

# Add project root to path so we can import ashlr_server
sys.path.insert(0, str(Path(__file__).parent.parent))

# Patch psutil.cpu_percent before importing ashlr_server (it runs at import time)
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server


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
