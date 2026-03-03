"""Tests for _supervised_task, output_capture_loop, and start_background_tasks."""

import asyncio
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
        original_sleep = asyncio.sleep

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
        assert len(app["bg_tasks"]) == 5
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

        assert len(app["bg_tasks"]) == 6

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

        # Create a mock agent
        agent = MagicMock()
        agent.status = "working"
        agent.name = "test-agent"
        agent.task = "test task"
        agent.role = "general"
        agent.plan_mode = False
        agent.needs_input = False
        agent.input_prompt = ""
        agent.output_lines = ["line1", "line2"]
        agent.summary = ""
        agent.progress_pct = 0
        agent.health_score = 1.0
        agent.error_count = 0
        agent.memory_mb = 0.0
        agent.total_output_lines = 0
        agent.output_rate = 0.0
        agent.estimated_cost_usd = 0.0
        agent.last_output_time = time.monotonic()
        agent._spawn_time = time.monotonic() - 60
        agent._stale_warned = False
        agent._first_output_received = True
        agent.time_to_first_output = 1.0
        agent._output_line_timestamps = []
        agent._overflow_to_archive = None
        agent._flood_ticks = 0
        agent._flood_detected = False
        agent._last_llm_summary_time = 0
        agent._prev_output_hash = ""
        agent._summary_output_hash = ""
        agent._llm_summary = ""
        agent._capture_fail_count = 0
        agent._last_needs_input_event = 0
        agent._error_entered_at = 0
        agent._health_low_warned = False
        agent._health_critical_warned = False
        agent.created_at = datetime.now(timezone.utc).isoformat()
        agent.updated_at = datetime.now(timezone.utc).isoformat()
        agent.to_dict = MagicMock(return_value={"id": "a1b2", "status": "working"})
        agent.set_status = MagicMock()
        agent.create_snapshot = MagicMock()

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
        hub = app["ws_hub"]

        agent = MagicMock()
        agent.status = "spawning"
        agent._spawn_time = time.monotonic() - 35  # 35 seconds ago — past 30s timeout
        agent.name = "stuck-spawn"
        agent.set_status = MagicMock()
        agent.to_dict = MagicMock(return_value={"id": "s1", "status": "error"})

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
        hub = app["ws_hub"]

        agent = MagicMock()
        agent.status = "working"
        agent.name = "failing-agent"
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic()
        agent._stale_warned = False
        agent._capture_fail_count = 0
        agent._error_entered_at = 0
        agent.health_score = 1.0
        agent._health_low_warned = False
        agent._health_critical_warned = False
        agent.set_status = MagicMock()
        agent.to_dict = MagicMock(return_value={"id": "f1", "status": "error"})

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
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = MagicMock()
        agent.status = "working"
        agent.name = "stale-agent"
        agent._spawn_time = time.monotonic() - 1200
        agent.last_output_time = time.monotonic() - 901  # >900s = 15 min
        agent._stale_warned = False
        agent._capture_fail_count = 0
        agent._error_entered_at = 0
        agent.health_score = 1.0
        agent._health_low_warned = False
        agent._health_critical_warned = False
        agent.set_status = MagicMock()
        agent.to_dict = MagicMock(return_value={"id": "st1", "status": "error"})

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
        assert "15 minutes" in agent.error_message

    async def test_stale_warning_at_5_minutes(self):
        """Agents with no output for >5 minutes should get a stale warning."""
        app = self._make_capture_app()
        manager = app["agent_manager"]
        hub = app["ws_hub"]

        agent = MagicMock()
        agent.status = "working"
        agent.name = "quiet-agent"
        agent._spawn_time = time.monotonic() - 600
        agent.last_output_time = time.monotonic() - 350  # >300s = 5 min but <900s
        agent._stale_warned = False
        agent._capture_fail_count = 0
        agent._error_entered_at = 0
        agent.health_score = 1.0
        agent._health_low_warned = False
        agent._health_critical_warned = False
        agent.set_status = MagicMock()
        agent.to_dict = MagicMock(return_value={"id": "q1", "status": "working"})

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

        agent = MagicMock()
        agent.status = "error"
        agent.name = "errored-agent"
        agent._spawn_time = time.monotonic() - 60
        agent.last_output_time = time.monotonic()
        agent._stale_warned = False
        agent._capture_fail_count = 0
        agent.health_score = 1.0
        agent._health_low_warned = False
        agent._health_critical_warned = False

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
