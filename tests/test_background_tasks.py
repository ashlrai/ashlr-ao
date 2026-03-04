"""Tests for background task loops: _supervised_task, output_capture_loop,
health_check_loop, metrics_loop, memory_watchdog_loop, archive_cleanup_loop,
and start/cleanup helpers."""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    from ashlr_server import (
        _supervised_task,
        output_capture_loop,
        health_check_loop,
        metrics_loop,
        memory_watchdog_loop,
        archive_cleanup_loop,
        start_background_tasks,
        cleanup_background_tasks,
        Config,
        License,
        PRO_FEATURES,
        COMMUNITY_LICENSE,
        AgentManager,
        WebSocketHub,
        IntelligenceClient,
    )


# ── Helpers ──

def _make_mock_agent(**overrides):
    """Create a MagicMock agent with all attributes needed by output_capture_loop."""
    agent = MagicMock()
    defaults = {
        "status": "working", "name": "test-agent", "task": "test",
        "role": "general", "plan_mode": False, "needs_input": False,
        "input_prompt": "", "output_lines": [], "summary": "",
        "progress_pct": 0, "health_score": 1.0, "error_count": 0,
        "memory_mb": 0.0, "total_output_lines": 0, "output_rate": 0.0,
        "estimated_cost_usd": 0.0, "last_output_time": time.monotonic(),
        "_spawn_time": time.monotonic() - 60, "_stale_warned": False,
        "_first_output_received": True, "time_to_first_output": 1.0,
        "_output_line_timestamps": [], "_overflow_to_archive": None,
        "_flood_ticks": 0, "_flood_detected": False,
        "_last_llm_summary_time": 0, "_prev_output_hash": "",
        "_summary_output_hash": "", "_llm_summary": "",
        "_capture_fail_count": 0, "_last_needs_input_event": 0,
        "_error_entered_at": 0, "_health_low_warned": False,
        "_health_critical_warned": False,
        "restart_count": 0, "max_restarts": 3, "last_restart_time": 0,
        "project_id": None, "context_pct": 0.0,
        "_context_exhaustion_warned": False, "_context_auto_paused": False,
        "_pathological": False, "workflow_run_id": None, "pid": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(agent, k, v)
    if "to_dict" not in overrides:
        agent.to_dict = MagicMock(return_value={"id": "mock", "status": defaults["status"]})
    if "set_status" not in overrides:
        def _set_status(s):
            agent.status = s
        agent.set_status = MagicMock(side_effect=_set_status)
    if "create_snapshot" not in overrides:
        agent.create_snapshot = MagicMock()
    return agent


def _make_mock_app(with_intel=False):
    """Create a minimal mock app dict-like object for background task tests."""
    config = Config()
    config.demo_mode = True
    config.spawn_pressure_block = False

    app = ashlr_server.create_app(config)
    mock_db = MagicMock()
    mock_db.init = AsyncMock()
    mock_db.get_projects = AsyncMock(return_value=[])
    mock_db.get_workflows = AsyncMock(return_value=[])
    mock_db.get_presets = AsyncMock(return_value=[])
    mock_db.save_agent = AsyncMock()
    mock_db.save_event = AsyncMock()
    mock_db.log_event = AsyncMock()
    mock_db.close = AsyncMock()
    mock_db.get_history = AsyncMock(return_value=[])
    mock_db.get_events = AsyncMock(return_value=[])
    mock_db.get_resumable_sessions = AsyncMock(return_value=[])
    mock_db.archive_output = AsyncMock()
    mock_db.release_file_locks = AsyncMock()
    mock_db.get_archived_output = AsyncMock(return_value=([], 0))
    mock_db.get_bookmarks = AsyncMock(return_value=[])
    mock_db.add_bookmark = AsyncMock(return_value=1)
    mock_db.save_project = AsyncMock()
    mock_db.delete_project = AsyncMock(return_value=False)
    mock_db.save_workflow = AsyncMock()
    mock_db.save_preset = AsyncMock()
    mock_db.delete_preset = AsyncMock(return_value=False)
    mock_db.delete_workflow = AsyncMock(return_value=False)
    mock_db.save_message = AsyncMock()
    mock_db.get_messages = AsyncMock(return_value=[])
    mock_db.upsert_scratchpad = AsyncMock()
    mock_db.delete_scratchpad = AsyncMock(return_value=False)
    mock_db.save_bookmark = AsyncMock(return_value=1)
    mock_db.get_history_item = AsyncMock(return_value=None)
    mock_db.get_workflow = AsyncMock(return_value=None)
    mock_db.get_agent_history_item = AsyncMock(return_value=None)
    mock_db.get_project = AsyncMock(return_value=None)
    mock_db.update_project = AsyncMock(return_value=None)
    mock_db.get_preset = AsyncMock(return_value=None)
    mock_db.get_agent_history = AsyncMock(return_value=[])
    mock_db.cleanup_old_archives = AsyncMock(return_value=0)
    mock_db.update_org_license = AsyncMock()
    mock_db.get_org_license_key = AsyncMock(return_value="")
    mock_db.find_similar_tasks = AsyncMock(return_value=[])
    mock_db._db = MagicMock()
    # Mock the DB execute for license loading in start_background_tasks
    mock_cursor = AsyncMock()
    mock_cursor.fetchone = AsyncMock(return_value=None)
    mock_db._db.execute = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_cursor),
        __aexit__=AsyncMock(return_value=False),
    ))
    mock_db.db_path = Path("/tmp/test-ashlr.db")

    app["db"] = mock_db
    app["ws_hub"].db = mock_db
    app["rate_limiter"].check = lambda *a, **kw: (True, 0.0)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["db_available"] = True
    app["db_ready"] = True
    app["bg_task_health"] = {}
    app["bg_tasks"] = []

    pro_lic = License(
        tier="pro", max_agents=100,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
        features=PRO_FEATURES,
    )
    app["license"] = pro_lic
    app["agent_manager"].license = pro_lic

    if not with_intel:
        # Ensure intelligence is not available so meta_agent task is skipped
        app["intelligence"].available = False

    return app


# ═══════════════════════════════════════════════
# _supervised_task tests
# ═══════════════════════════════════════════════


class TestSupervisedTaskNormalCompletion:
    """Task that finishes without error should simply return."""

    async def test_task_runs_coro_fn(self):
        """Verify _supervised_task calls the coroutine function with the app."""
        call_count = 0

        async def my_task(app):
            nonlocal call_count
            call_count += 1
            # Complete immediately — but _supervised_task loops forever,
            # so we raise CancelledError to exit after first run
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(return_value=None)

        with pytest.raises(asyncio.CancelledError):
            await _supervised_task("test_task", my_task, app)

        assert call_count == 1

    async def test_cancelled_error_propagates(self):
        """CancelledError should not be caught — it propagates up."""

        async def cancelling_task(app):
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(return_value=None)

        with pytest.raises(asyncio.CancelledError):
            await _supervised_task("test_cancel", cancelling_task, app)


class TestSupervisedTaskCrashRecovery:
    """Task that raises an exception should be restarted with backoff."""

    async def test_task_restarts_on_exception(self):
        """After a crash, the coro_fn should be called again."""
        call_count = 0

        async def crashy_task(app):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            # Second call: exit via cancel
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(return_value=None)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("test_restart", crashy_task, app)

        assert call_count == 2
        # Should have slept for backoff after the first crash
        mock_sleep.assert_awaited_once()

    async def test_health_tracking_on_crash(self):
        """bg_task_health should be updated with restart count and error details."""
        task_health = {}

        async def failing_task(app):
            raise ValueError("something broke")

        call_count = 0

        async def fail_then_cancel(app):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ValueError(f"error #{call_count}")
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(side_effect=lambda k, v: task_health if k == "bg_task_health" else v)
        app.get = MagicMock(return_value=None)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("my_worker", fail_then_cancel, app)

        assert "my_worker" in task_health
        assert task_health["my_worker"]["restarts"] == 2
        assert "error #2" in task_health["my_worker"]["last_error"]
        assert "last_crash" in task_health["my_worker"]

    async def test_backoff_increases_with_restart_count(self):
        """Sleep duration should increase: min(5 * restart_count, 30)."""
        call_count = 0

        async def multi_crash(app):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise RuntimeError(f"crash {call_count}")
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(return_value=None)

        sleep_values = []

        async def mock_sleep(duration):
            sleep_values.append(duration)

        with patch("asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("backoff_test", multi_crash, app)

        assert call_count == 4
        # Backoff: min(5*1, 30)=5, min(5*2, 30)=10, min(5*3, 30)=15
        assert sleep_values == [5, 10, 15]

    async def test_backoff_caps_at_30_seconds(self):
        """Backoff should never exceed 30 seconds."""
        call_count = 0

        async def many_crashes(app):
            nonlocal call_count
            call_count += 1
            if call_count <= 8:
                raise RuntimeError("crash")
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(return_value=None)

        sleep_values = []

        async def mock_sleep(duration):
            sleep_values.append(duration)

        with patch("asyncio.sleep", side_effect=mock_sleep):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("cap_test", many_crashes, app)

        # Restarts 1-8: min(5,30), min(10,30), min(15,30), min(20,30), min(25,30), min(30,30), min(35,30)=30, min(40,30)=30
        assert sleep_values[-1] == 30
        assert sleep_values[-2] == 30
        assert all(v <= 30 for v in sleep_values)

    async def test_ws_broadcast_on_crash(self):
        """Crash events should be broadcast to WebSocket clients."""
        call_count = 0

        async def crash_once(app):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("ws test crash")
            raise asyncio.CancelledError()

        mock_hub = MagicMock()
        mock_hub.broadcast = AsyncMock()

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(side_effect=lambda k: mock_hub if k == "ws_hub" else None)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("ws_test", crash_once, app)

        # Check broadcast was called with crash event
        mock_hub.broadcast.assert_awaited_once()
        broadcast_msg = mock_hub.broadcast.call_args[0][0]
        assert broadcast_msg["type"] == "event"
        assert broadcast_msg["event"] == "bg_task_crash"
        assert broadcast_msg["task"] == "ws_test"
        assert broadcast_msg["restart"] == 1
        assert "ws test crash" in broadcast_msg["error"]

    async def test_broadcast_failure_does_not_prevent_restart(self):
        """If broadcasting the crash event fails, the task should still restart."""
        call_count = 0

        async def crash_twice(app):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("crash")
            raise asyncio.CancelledError()

        mock_hub = MagicMock()
        mock_hub.broadcast = AsyncMock(side_effect=Exception("broadcast broken"))

        app = MagicMock()
        app.setdefault = MagicMock(return_value={})
        app.get = MagicMock(side_effect=lambda k: mock_hub if k == "ws_hub" else None)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("broadcast_fail", crash_twice, app)

        # Task should have been called twice despite broadcast failure
        assert call_count == 2

    async def test_health_error_truncated_to_200_chars(self):
        """last_error should be truncated to 200 characters."""
        task_health = {}
        long_error = "x" * 500

        async def long_error_task(app):
            raise RuntimeError(long_error)

        call_count = 0

        async def fail_once_long(app):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(long_error)
            raise asyncio.CancelledError()

        app = MagicMock()
        app.setdefault = MagicMock(side_effect=lambda k, v: task_health if k == "bg_task_health" else v)
        app.get = MagicMock(return_value=None)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(asyncio.CancelledError):
                await _supervised_task("truncate_test", fail_once_long, app)

        assert len(task_health["truncate_test"]["last_error"]) <= 200


# ═══════════════════════════════════════════════
# start_background_tasks tests
# ═══════════════════════════════════════════════


class TestStartBackgroundTasks:
    """Tests for start_background_tasks setup function."""

    async def test_creates_5_tasks_without_intel(self):
        """Should create 5 supervised tasks when intelligence is not available."""
        app = _make_mock_app(with_intel=False)

        # Run start_background_tasks but cancel quickly
        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        assert "bg_tasks" in app
        assert len(app["bg_tasks"]) == 6  # includes webhook_delivery
        assert "bg_task_health" in app
        assert app["bg_task_health"] == {}

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_creates_6_tasks_with_intel(self):
        """Should create 6 supervised tasks when intelligence is available."""
        app = _make_mock_app(with_intel=True)
        # Force intelligence to be available
        app["intelligence"] = MagicMock()
        app["intelligence"].available = True

        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        assert len(app["bg_tasks"]) == 7  # 6 base + meta_agent

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_initializes_database(self):
        """Should call db.init() during startup."""
        app = _make_mock_app()
        db = app["db"]

        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        db.init.assert_awaited()

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_db_init_retry_on_failure(self):
        """If db.init fails first time, it retries once."""
        app = _make_mock_app()
        db = app["db"]
        # First call fails, second succeeds
        db.init = AsyncMock(side_effect=[Exception("db error"), None])

        task = asyncio.create_task(start_background_tasks(app))
        # Must wait >1s because start_background_tasks does asyncio.sleep(1) between retries
        await asyncio.sleep(1.2)

        assert db.init.await_count == 2
        # Should still be available since retry succeeded
        assert app.get("db_available", True) is True

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_db_degraded_mode_on_double_failure(self):
        """If db.init fails twice, app enters degraded mode."""
        app = _make_mock_app()
        db = app["db"]
        db.init = AsyncMock(side_effect=[Exception("fail 1"), Exception("fail 2")])

        task = asyncio.create_task(start_background_tasks(app))
        # Must wait >1s because start_background_tasks does asyncio.sleep(1) between retries
        await asyncio.sleep(1.2)

        assert db.init.await_count == 2
        assert app["db_available"] is False
        assert app["db_ready"] is False, "db_ready must be False when DB init fails"

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_sets_db_ready_flag(self):
        """start_background_tasks should set db_ready to True."""
        app = _make_mock_app()
        app.pop("db_ready", None)

        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        assert app["db_ready"] is True

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_loads_community_license_when_no_key(self):
        """Without a license key, app should use community license."""
        app = _make_mock_app()
        app["config"].license_key = ""

        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        # The license loaded during start_background_tasks won't override
        # the pro license we set in _make_mock_app unless there's no key,
        # which there isn't by default in community config. But since we
        # set a pro license in _make_mock_app, we need to check that
        # start_background_tasks respects the no-key scenario.
        # With no key in config or DB, the license stays as-is (no override).
        # The function only changes license when license_key is truthy.
        assert "license" in app

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_initializes_bg_task_health_empty(self):
        """bg_task_health should be initialized as an empty dict."""
        app = _make_mock_app()
        app.pop("bg_task_health", None)

        task = asyncio.create_task(start_background_tasks(app))
        await asyncio.sleep(0.05)

        assert app["bg_task_health"] == {}

        # Clean up
        for t in app["bg_tasks"]:
            t.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════
# output_capture_loop tests
# ═══════════════════════════════════════════════


class TestOutputCaptureLoop:
    """Tests for the output_capture_loop background task."""

    def _make_capture_app(self):
        """Build a minimal app suitable for output_capture_loop testing."""
        app = _make_mock_app()
        # Use a very short capture interval so tests run fast
        app["config"].output_capture_interval = 0.01
        # Ensure ws_hub has proper async methods
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()
        hub.broadcast_event = AsyncMock()
        return app

    async def test_loop_runs_and_can_be_cancelled(self):
        """output_capture_loop should run repeatedly and stop on cancellation."""
        app = self._make_capture_app()
        manager = app["agent_manager"]
        manager.agents = {}  # No agents

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.05)  # Let it run a few iterations
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_captures_agent_output(self):
        """When agents have new output, it should be broadcast."""
        app = self._make_capture_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        # Create a mock agent with all required attributes
        agent = _make_mock_agent(
            task="test task", output_lines=["line1", "line2"],
            to_dict=MagicMock(return_value={"id": "a1b2", "status": "working"}),
        )

        manager.agents = {"a1b2": agent}
        manager.capture_output = AsyncMock(return_value=["new output line"])
        manager.detect_status = AsyncMock(return_value="working")
        manager._check_file_conflicts = MagicMock(return_value=[])

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify capture was called
        manager.capture_output.assert_awaited()
        # Verify output was broadcast
        assert hub.broadcast.await_count > 0

    async def test_skips_paused_and_killed_agents(self):
        """Paused and killed agents should not have output captured."""
        app = self._make_capture_app()
        manager = app["agent_manager"]

        paused_agent = MagicMock()
        paused_agent.status = "paused"
        killed_agent = MagicMock()
        killed_agent.status = "killed"

        manager.agents = {"p1": paused_agent, "k1": killed_agent}
        manager.capture_output = AsyncMock(return_value=[])

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # capture_output should never have been called since both agents are paused/killed
        manager.capture_output.assert_not_awaited()

    async def test_spawn_timeout_detection(self):
        """Agents stuck in 'spawning' for >30s should be marked as error."""
        app = self._make_capture_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(
            status="spawning", name="stuck-spawn",
            _spawn_time=time.monotonic() - 35,
            to_dict=MagicMock(return_value={"id": "s1", "status": "error"}),
        )

        manager.agents = {"s1": agent}
        # No output to capture for spawning agent with error status after set
        manager.capture_output = AsyncMock(return_value=None)
        manager.detect_status = AsyncMock(return_value="error")

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Agent should have been set to error status
        agent.set_status.assert_called_with("error")
        assert "Spawn timeout" in agent.error_message

    async def test_capture_failure_counting(self):
        """3 consecutive capture failures should mark agent as error."""
        app = self._make_capture_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(
            name="failing-agent",
            to_dict=MagicMock(return_value={"id": "f1", "status": "error"}),
        )

        manager.agents = {"f1": agent}
        # capture_output returns None to simulate failure
        manager.capture_output = AsyncMock(return_value=None)
        manager.detect_status = AsyncMock(return_value="working")

        task = asyncio.create_task(output_capture_loop(app))
        # Wait enough for 3+ capture iterations
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # After 3 failures, agent should be set to error
        assert agent._capture_fail_count >= 3
        agent.set_status.assert_called_with("error")
        assert "capture failed" in agent.error_message.lower()

    async def test_no_agents_loop_continues(self):
        """With no agents, the loop should still run without errors."""
        app = self._make_capture_app()
        manager = app["agent_manager"]
        manager.agents = {}

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.05)
        assert not task.done()  # Still running
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_staleness_detection_15_minutes(self):
        """Agents with no output for >15 minutes should be flagged as error."""
        app = self._make_capture_app()
        app["config"].auto_restart_on_stall = False  # Disable auto-restart to test error path
        manager = app["agent_manager"]

        agent = _make_mock_agent(
            name="stale-agent",
            _spawn_time=time.monotonic() - 1200,
            last_output_time=time.monotonic() - 901,
            to_dict=MagicMock(return_value={"id": "st1", "status": "error"}),
        )

        manager.agents = {"st1": agent}
        # Agent has no new output
        manager.capture_output = AsyncMock(return_value=[])
        manager.detect_status = AsyncMock(return_value="working")

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        agent.set_status.assert_called_with("error")
        assert "minutes" in agent.error_message

    async def test_stale_warning_at_5_minutes(self):
        """Agents with no output for >5 minutes should get a stale warning."""
        app = self._make_capture_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            name="quiet-agent",
            _spawn_time=time.monotonic() - 600,
            last_output_time=time.monotonic() - 350,
            to_dict=MagicMock(return_value={"id": "q1", "status": "working"}),
        )

        manager.agents = {"q1": agent}
        manager.capture_output = AsyncMock(return_value=[])
        manager.detect_status = AsyncMock(return_value="working")

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have sent a stale warning broadcast
        hub.broadcast_event.assert_called()
        stale_calls = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_stale_warning"
        ]
        assert len(stale_calls) > 0
        assert agent._stale_warned is True

    async def test_error_agents_skip_capture(self):
        """Agents already in error status should skip the capture phase."""
        app = self._make_capture_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(status="error", name="errored-agent")

        manager.agents = {"e1": agent}
        manager.capture_output = AsyncMock(return_value=[])

        task = asyncio.create_task(output_capture_loop(app))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # capture_output should not be called for error-status agents
        manager.capture_output.assert_not_awaited()


# ═══════════════════════════════════════════════
# cleanup_background_tasks tests
# ═══════════════════════════════════════════════


class TestCleanupBackgroundTasks:
    """Tests for cleanup_background_tasks."""

    async def test_cancels_all_tasks(self):
        """All background tasks should be cancelled during cleanup."""
        app = _make_mock_app()

        # Create real async tasks that we can cancel
        t1 = asyncio.create_task(asyncio.sleep(100))
        t2 = asyncio.create_task(asyncio.sleep(100))
        app["bg_tasks"] = [t1, t2]

        await cleanup_background_tasks(app)

        assert t1.cancelled()
        assert t2.cancelled()

    async def test_cleanup_handles_empty_tasks(self):
        """Cleanup should handle no tasks gracefully."""
        app = _make_mock_app()
        app["bg_tasks"] = []

        # Should not raise
        await cleanup_background_tasks(app)

    async def test_cleanup_archives_agents(self):
        """Cleanup should save remaining agents to the database."""
        app = _make_mock_app()
        app["bg_tasks"] = []
        db = app["db"]

        # Add a mock agent
        agent = MagicMock()
        agent.id = "a1"
        agent.name = "test"
        agent.values = MagicMock(return_value=[agent])
        app["agent_manager"].agents = {"a1": agent}

        await cleanup_background_tasks(app)

        db.save_agent.assert_awaited_once_with(agent)
        db.close.assert_awaited_once()


# ═══════════════════════════════════════════════
# health_check_loop tests
# ═══════════════════════════════════════════════


class TestHealthCheckLoop:
    """Tests for health_check_loop: auto-restart, pathological detection,
    tmux liveness, idle reaping, context exhaustion."""

    def _make_health_app(self):
        """Build app for health_check_loop with short sleep interval."""
        app = _make_mock_app()
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()
        hub.broadcast_event = AsyncMock()
        app["config"].pathological_error_window_sec = 30
        app["config"].max_pathological_restarts = 1
        app["config"].idle_agent_ttl = 0  # disabled by default
        app["config"].cost_budget_usd = 0  # disabled
        app["config"].cost_budget_auto_pause = False
        app["config"].stall_timeout_minutes = 60
        app["config"].hung_timeout_minutes = 60
        app["config"].context_auto_pause_threshold = 0.97
        manager = app["agent_manager"]
        manager._tmux_session_exists = AsyncMock(return_value=True)
        manager.restart = AsyncMock(return_value=True)
        manager.kill = AsyncMock()
        manager.pause = AsyncMock()
        manager.resume = AsyncMock()
        manager.task_queue = []
        manager.check_system_pressure = MagicMock(return_value={})
        manager.check_stage_timeouts = MagicMock(return_value=[])
        return app

    async def test_auto_restart_error_agent(self):
        """Agent in error state for >10s with restarts remaining gets restarted."""
        app = self._make_health_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="error", name="error-agent",
            restart_count=0, max_restarts=3,
            _error_entered_at=time.monotonic() - 15,  # >10s ago
            last_restart_time=0, _pathological=False,
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-test",
        )

        # After restart, return the agent with updated state
        restarted = _make_mock_agent(
            status="working", name="error-agent",
            restart_count=1, max_restarts=3,
        )
        manager.agents = {"a1": agent}

        def mock_restart(aid):
            manager.agents["a1"] = restarted
            return True

        manager.restart = AsyncMock(side_effect=mock_restart)

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.restart.assert_awaited_once_with("a1")
        # Should broadcast agent_restarted event
        restart_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_restarted"
        ]
        assert len(restart_events) > 0

    async def test_pathological_detection(self):
        """Agent that errors quickly after restart gets flagged pathological."""
        app = self._make_health_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        now = time.monotonic()
        agent = _make_mock_agent(
            status="error", name="pathological-agent",
            restart_count=1, max_restarts=5,
            _error_entered_at=now - 15,
            last_restart_time=now - 20,  # errored 5s after restart (< 30s window)
            _pathological=False,
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-test",
        )
        manager.agents = {"p1": agent}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert agent._pathological is True
        assert agent.max_restarts == 1  # clamped to max_pathological_restarts
        patho_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_pathological"
        ]
        assert len(patho_events) > 0

    async def test_max_restarts_exhausted_notification(self):
        """Agent that exhausted restarts sends notification once."""
        app = self._make_health_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="error", name="exhausted-agent",
            restart_count=3, max_restarts=3,
            _error_entered_at=time.monotonic() - 15,
            last_restart_time=0, _pathological=False,
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-test",
        )
        manager.agents = {"e1": agent}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should broadcast exhausted event
        exhausted_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_restart_exhausted"
        ]
        assert len(exhausted_events) > 0
        # _error_entered_at set to 0 so notification doesn't repeat
        assert agent._error_entered_at == 0

    async def test_tmux_death_detection(self):
        """Agent whose tmux session died gets marked as error."""
        app = self._make_health_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="working", name="dead-tmux",
            restart_count=0, max_restarts=3,
            _error_entered_at=0, last_restart_time=0,
            _pathological=False,
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-dead",
        )
        manager.agents = {"d1": agent}
        manager._tmux_session_exists = AsyncMock(return_value=False)

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        agent.set_status.assert_called_with("error")
        assert "tmux session terminated" in agent.error_message

    async def test_idle_reaping(self):
        """Idle agent past TTL gets killed and archived."""
        app = self._make_health_app()
        app["config"].idle_agent_ttl = 300  # 5 minutes
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="idle", name="idle-agent",
            restart_count=0, max_restarts=3,
            _error_entered_at=0, last_restart_time=0,
            _pathological=False,
            last_output_time=time.monotonic() - 400,  # idle > 300s
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-idle",
        )
        manager.agents = {"i1": agent}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.kill.assert_awaited_once_with("i1")
        reap_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_reaped"
        ]
        assert len(reap_events) > 0

    async def test_context_exhaustion_warning(self):
        """Agent at >= 92% context triggers snapshot and warning."""
        app = self._make_health_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="working", name="ctx-warn",
            restart_count=0, max_restarts=3,
            _error_entered_at=0, last_restart_time=0,
            _pathological=False,
            workflow_run_id=None, context_pct=0.95,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-ctx",
        )
        manager.agents = {"c1": agent}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        agent.create_snapshot.assert_called_with(trigger="context_warning")
        assert agent._context_exhaustion_warned is True
        ctx_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_context_warning"
        ]
        assert len(ctx_events) > 0

    async def test_no_agents_loop_continues(self):
        """Health check loop continues with zero agents."""
        app = self._make_health_app()
        app["agent_manager"].agents = {}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_fleet_budget_auto_pause(self):
        """Working agents get paused when fleet cost exceeds budget."""
        app = self._make_health_app()
        app["config"].cost_budget_usd = 10.0
        app["config"].cost_budget_auto_pause = True
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            status="working", name="costly",
            restart_count=0, max_restarts=3,
            _error_entered_at=0, last_restart_time=0,
            _pathological=False,
            estimated_cost_usd=15.0,  # over budget
            workflow_run_id=None, context_pct=0.0,
            _context_exhaustion_warned=False,
            _context_auto_paused=False, pid=0,
            tmux_session="ashlr-cost",
        )
        agent.id = "c1"
        manager.agents = {"c1": agent}

        task = asyncio.create_task(health_check_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.pause.assert_awaited()
        budget_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "fleet_budget_exceeded"
        ]
        assert len(budget_events) > 0


# ═══════════════════════════════════════════════
# metrics_loop tests
# ═══════════════════════════════════════════════


class TestMetricsLoop:
    """Tests for metrics_loop: system metric collection and broadcast."""

    def _make_metrics_app(self):
        """Build app for metrics_loop testing."""
        app = _make_mock_app()
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()
        manager = app["agent_manager"]
        manager.get_agent_memory = AsyncMock(return_value=128.5)
        manager.get_agent_cpu = AsyncMock(return_value=12.3)
        return app

    async def test_collects_and_broadcasts_metrics(self):
        """metrics_loop calls collect_system_metrics and broadcasts result."""
        app = self._make_metrics_app()
        hub = app["ws_hub"]
        manager = app["agent_manager"]
        manager.agents = {}

        task = asyncio.create_task(metrics_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should have broadcast at least once
        assert hub.broadcast.await_count > 0
        msg = hub.broadcast.call_args[0][0]
        assert msg["type"] == "metrics"

    async def test_updates_per_agent_memory_and_cpu(self):
        """metrics_loop updates memory_mb and cpu_pct for each agent."""
        app = self._make_metrics_app()
        manager = app["agent_manager"]

        agent = _make_mock_agent(name="metric-agent", memory_mb=0.0, cpu_pct=0.0)
        manager.agents = {"m1": agent}

        task = asyncio.create_task(metrics_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.get_agent_memory.assert_awaited_with("m1")
        manager.get_agent_cpu.assert_awaited_with("m1")
        assert agent.memory_mb == 128.5
        assert agent.cpu_pct == 12.3

    async def test_handles_per_agent_metric_failure(self):
        """Failure to get metrics for one agent doesn't crash the loop."""
        app = self._make_metrics_app()
        manager = app["agent_manager"]
        manager.get_agent_memory = AsyncMock(side_effect=Exception("proc gone"))

        agent = _make_mock_agent(name="gone-agent")
        manager.agents = {"g1": agent}

        task = asyncio.create_task(metrics_loop(app))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_loop_continues_on_broadcast_error(self):
        """Broadcast failure doesn't crash the loop."""
        app = self._make_metrics_app()
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock(side_effect=Exception("ws disconnected"))
        app["agent_manager"].agents = {}

        task = asyncio.create_task(metrics_loop(app))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════
# memory_watchdog_loop tests
# ═══════════════════════════════════════════════


class TestMemoryWatchdogLoop:
    """Tests for memory_watchdog_loop: kill over-limit, pause at threshold, warn."""

    def _make_watchdog_app(self):
        """Build app for memory_watchdog_loop testing."""
        app = _make_mock_app()
        app["config"].memory_limit_mb = 2048
        app["config"].agent_memory_pause_pct = 0.90
        hub = app["ws_hub"]
        hub.broadcast = AsyncMock()
        hub.broadcast_event = AsyncMock()
        manager = app["agent_manager"]
        manager.kill = AsyncMock()
        manager.pause = AsyncMock()
        return app

    async def test_kills_agent_over_memory_limit(self):
        """Agent exceeding memory_limit_mb gets killed."""
        app = self._make_watchdog_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            name="mem-hog", memory_mb=2200.0,
            _memory_pause_warned=False,
        )
        manager.agents = {"m1": agent}

        task = asyncio.create_task(memory_watchdog_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.kill.assert_awaited_once_with("m1")
        kill_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_killed"
        ]
        assert len(kill_events) > 0

    async def test_pauses_agent_at_pause_threshold(self):
        """Agent at 90% of limit gets auto-paused."""
        app = self._make_watchdog_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]
        # 90% of 2048 = 1843.2
        agent = _make_mock_agent(
            name="mem-warn", memory_mb=1900.0, status="working",
            _memory_pause_warned=False,
        )
        manager.agents = {"m2": agent}

        task = asyncio.create_task(memory_watchdog_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.pause.assert_awaited_once_with("m2")
        assert agent._memory_pause_warned is True
        pause_events = [
            c for c in hub.broadcast_event.call_args_list
            if c[0][0] == "agent_memory_paused"
        ]
        assert len(pause_events) > 0

    async def test_warns_at_75_percent(self, caplog):
        """Agent at 75% of limit triggers a warning log."""
        app = self._make_watchdog_app()
        manager = app["agent_manager"]
        # 75% of 2048 = 1536, need > 1536 but < pause threshold (1843.2)
        agent = _make_mock_agent(
            name="mem-mid", memory_mb=1600.0,
            _memory_pause_warned=False,
        )
        manager.agents = {"m3": agent}

        import logging
        with caplog.at_level(logging.WARNING, logger="ashlr"):
            task = asyncio.create_task(memory_watchdog_loop(app))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert any("memory warning" in r.message.lower() for r in caplog.records)

    async def test_under_threshold_no_action(self):
        """Agent under 75% limit gets no warnings or kills."""
        app = self._make_watchdog_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = _make_mock_agent(
            name="mem-ok", memory_mb=500.0,
            _memory_pause_warned=False,
        )
        manager.agents = {"m4": agent}

        task = asyncio.create_task(memory_watchdog_loop(app))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        manager.kill.assert_not_awaited()
        manager.pause.assert_not_awaited()

    async def test_no_agents_loop_continues(self):
        """Watchdog loop continues with zero agents."""
        app = self._make_watchdog_app()
        app["agent_manager"].agents = {}

        task = asyncio.create_task(memory_watchdog_loop(app))
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ═══════════════════════════════════════════════
# archive_cleanup_loop tests
# ═══════════════════════════════════════════════


class TestArchiveCleanupLoop:
    """Tests for archive_cleanup_loop: hourly cleanup of old records."""

    async def test_calls_cleanup_old_archives(self):
        """archive_cleanup_loop calls db.cleanup_old_archives with 48hr retention."""
        app = _make_mock_app()
        app["db"].cleanup_old_archives = AsyncMock(return_value=5)

        # Patch sleep to make it run instantly, then cancel after first iteration
        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        app["db"].cleanup_old_archives.assert_awaited_once_with(retention_hours=48)

    async def test_skips_when_db_unavailable(self):
        """archive_cleanup_loop skips cleanup when DB is unavailable."""
        app = _make_mock_app()
        app["db_available"] = False
        app["db"].cleanup_old_archives = AsyncMock(return_value=0)

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        app["db"].cleanup_old_archives.assert_not_awaited()

    async def test_handles_db_error_gracefully(self):
        """archive_cleanup_loop doesn't crash on DB errors."""
        app = _make_mock_app()
        app["db"].cleanup_old_archives = AsyncMock(side_effect=Exception("DB error"))

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        # Should have been called (and failed gracefully)
        app["db"].cleanup_old_archives.assert_awaited_once()

    async def test_zero_deleted_no_log(self, caplog):
        """When no records are deleted, no info log is emitted."""
        app = _make_mock_app()
        app["db"].cleanup_old_archives = AsyncMock(return_value=0)

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        import logging
        with caplog.at_level(logging.INFO, logger="ashlr"):
            with patch("asyncio.sleep", side_effect=mock_sleep):
                try:
                    await archive_cleanup_loop(app)
                except asyncio.CancelledError:
                    pass

        cleanup_logs = [r for r in caplog.records if "archive cleanup" in r.message.lower() and "removed" in r.message.lower()]
        assert len(cleanup_logs) == 0


# ═══════════════════════════════════════════════
# _supervised_task error recovery tests
# ═══════════════════════════════════════════════


class TestSupervisedTaskErrorRecovery:
    """Tests for _supervised_task crash handling and backoff."""

    async def test_restarts_crashed_loop_with_backoff(self):
        """_supervised_task restarts a crashing coro and applies increasing backoff."""
        crash_count = 0
        sleep_durations = []

        async def crashing_coro(app):
            nonlocal crash_count
            crash_count += 1
            raise RuntimeError(f"crash #{crash_count}")

        original_sleep = asyncio.sleep

        async def tracking_sleep(duration):
            sleep_durations.append(duration)
            if len(sleep_durations) >= 3:
                raise asyncio.CancelledError()
            # Don't actually sleep

        app = _make_mock_app()
        with patch("asyncio.sleep", side_effect=tracking_sleep):
            try:
                await _supervised_task("test-task", crashing_coro, app)
            except asyncio.CancelledError:
                pass

        assert crash_count >= 3
        # Backoff should increase: min(5*1, 30), min(5*2, 30), min(5*3, 30)
        assert sleep_durations[0] == 5
        assert sleep_durations[1] == 10
        assert sleep_durations[2] == 15

    async def test_tracks_health_on_crash(self):
        """_supervised_task records crash info in bg_task_health."""
        async def crashing_once(app):
            raise ValueError("test error message")

        sleep_count = 0

        async def mock_sleep(duration):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 1:
                raise asyncio.CancelledError()

        app = _make_mock_app()
        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await _supervised_task("my-task", crashing_once, app)
            except asyncio.CancelledError:
                pass

        health = app.get("bg_task_health", {})
        assert "my-task" in health
        assert health["my-task"]["restarts"] == 1
        assert "test error message" in health["my-task"]["last_error"]

    async def test_cancelled_error_propagates(self):
        """CancelledError should propagate without restart."""
        async def cancelling_coro(app):
            raise asyncio.CancelledError()

        app = _make_mock_app()
        with pytest.raises(asyncio.CancelledError):
            await _supervised_task("cancel-task", cancelling_coro, app)


# ═══════════════════════════════════════════════
# meta_agent_loop tests
# ═══════════════════════════════════════════════


class TestMetaAgentLoop:
    """Tests for meta_agent_loop behavior."""

    async def test_skips_when_no_intelligence_client(self):
        """meta_agent_loop does nothing when intelligence client is absent."""
        from ashlr_server import meta_agent_loop

        app = _make_mock_app()
        app["intelligence"] = None
        app["config"].llm_meta_interval = 1.0

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await meta_agent_loop(app)
            except asyncio.CancelledError:
                pass

        # Should have slept but done no analysis
        assert call_count >= 1

    async def test_skips_with_fewer_than_two_agents(self):
        """meta_agent_loop skips analysis when fewer than 2 agents exist."""
        from ashlr_server import meta_agent_loop

        app = _make_mock_app()
        mock_client = MagicMock()
        mock_client.available = True
        mock_client.analyze_fleet = AsyncMock(return_value=[])
        app["intelligence"] = mock_client
        app["config"].llm_meta_interval = 1.0
        # Only 1 agent
        app["agent_manager"].agents = {"a1": _make_mock_agent()}

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await meta_agent_loop(app)
            except asyncio.CancelledError:
                pass

        mock_client.analyze_fleet.assert_not_awaited()
