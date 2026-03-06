"""Tests for stream-json (--print mode) subprocess agent support."""

import asyncio
import collections
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server

from ashlr_server import Agent, AgentManager, Config, BackendConfig, KNOWN_BACKENDS
from tests.conftest import make_test_app, make_mock_db, TEST_WORKING_DIR


# ── Helpers ──

def make_stream_agent(agent_id="s001", status="working", **kwargs):
    """Create an Agent configured for stream-json mode."""
    defaults = dict(
        id=agent_id,
        name=f"stream-{agent_id}",
        role="general",
        status=status,
        working_dir="/tmp/test",
        backend="claude-code",
        task="Test stream task",
        output_mode="stream-json",
        _spawn_time=time.monotonic(),
    )
    defaults.update(kwargs)
    return Agent(**defaults)


class FakeProcess:
    """Mock asyncio.subprocess.Process with controllable stdout."""

    def __init__(self, lines=None, returncode=0):
        self.returncode = returncode
        self.pid = 12345
        self._lines = lines or []
        self._stdout_iter = iter(self._lines)
        self.stdout = self._make_stdout()
        self.stderr = self._make_stderr()
        self._terminated = False
        self._killed = False

    def _make_stdout(self):
        lines = self._lines

        class FakeStdout:
            def __aiter__(self_inner):
                return self_inner

            async def __anext__(self_inner):
                try:
                    return next(self._stdout_iter)
                except StopIteration:
                    raise StopAsyncIteration

        return FakeStdout()

    def _make_stderr(self):
        class FakeStderr:
            async def read(self_inner):
                return b""

        return FakeStderr()

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True

    async def wait(self):
        return self.returncode


# ── Model Tests ──

class TestAgentModelStreamFields:
    """Test Agent dataclass stream-json fields."""

    def test_default_output_mode_is_tmux(self):
        agent = Agent(
            id="x", name="x", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        assert agent.output_mode == "tmux"

    def test_stream_json_output_mode(self):
        agent = make_stream_agent()
        assert agent.output_mode == "stream-json"
        assert agent._subprocess_proc is None
        assert agent._reader_task is None

    def test_to_dict_includes_output_mode(self):
        agent = make_stream_agent()
        d = agent.to_dict()
        assert d["output_mode"] == "stream-json"
        assert d["cost_is_estimated"] is False  # stream-json gets real costs

    def test_to_dict_tmux_cost_is_estimated(self):
        agent = Agent(
            id="x", name="x", role="general", status="working",
            working_dir="/tmp", backend="claude-code", task="test",
        )
        d = agent.to_dict()
        assert d["output_mode"] == "tmux"
        assert d["cost_is_estimated"] is True


# ── Command Builder Tests ──

class TestBuildStreamJsonCommand:
    """Test _build_stream_json_command static method."""

    def test_basic_command(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "hello world")
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert cmd[-1] == "hello world"

    def test_with_model(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "task", model="sonnet")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "sonnet"

    def test_with_tools(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "task", tools=["Bash", "Read"])
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Bash,Read"

    def test_with_system_prompt(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "task", system_prompt="Be helpful")
        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == "Be helpful"

    def test_no_dangerously_skip_permissions(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "task")
        assert "--dangerously-skip-permissions" not in cmd

    def test_empty_task(self):
        bc = KNOWN_BACKENDS["claude-code"]
        cmd = AgentManager._build_stream_json_command(bc, "")
        # No task appended
        assert cmd[-1] == "stream-json"

    def test_unsupported_backend_skips_flags(self):
        bc = BackendConfig(command="codex", supports_model_select=False,
                           supports_system_prompt=False, supports_tool_restriction=False)
        cmd = AgentManager._build_stream_json_command(bc, "task", model="x", tools=["y"], system_prompt="z")
        assert "--model" not in cmd
        assert "--allowedTools" not in cmd
        assert "--append-system-prompt" not in cmd


# ── Stream Reader Tests ──

class TestReadStreamJson:
    """Test _read_stream_json coroutine."""

    @pytest.mark.asyncio
    async def test_system_event(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-123"})
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.session_id == "sess-123"
        assert any("[system]" in line for line in agent.output_lines)

    @pytest.mark.asyncio
    async def test_assistant_text_event(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello world\nSecond line"}]
            }
        })
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        lines = list(agent.output_lines)
        assert "Hello world" in lines
        assert "Second line" in lines
        assert agent._total_chars > 0

    @pytest.mark.asyncio
    async def test_tool_use_event(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"}
                }]
            }
        })
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        lines = list(agent.output_lines)
        assert any("[tool] Edit:" in line for line in lines)
        assert "/tmp/test.py" in agent._files_touched_set
        assert agent.files_touched == 1

    @pytest.mark.asyncio
    async def test_tool_result_error(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_result", "content": "File not found", "is_error": True}]
            }
        })
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.error_count == 1
        assert any("[tool_error]" in line for line in agent.output_lines)

    @pytest.mark.asyncio
    async def test_result_event_cost_extraction(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({
            "type": "result",
            "cost_usd": 0.0123,
            "duration_ms": 5000,
            "is_error": False,
            "num_turns": 3,
            "session_id": "sess-456",
        })
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.estimated_cost_usd == 0.0123
        assert agent.session_id == "sess-456"
        assert agent.status == "complete"

    @pytest.mark.asyncio
    async def test_result_event_with_usage(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        event = json.dumps({
            "type": "result",
            "total_cost_usd": 0.05,
            "duration_ms": 10000,
            "is_error": False,
            "num_turns": 5,
            "usage": {"input_tokens": 15000, "output_tokens": 3000},
        })
        proc = FakeProcess(lines=[event.encode() + b"\n"], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.tokens_input == 15000
        assert agent.tokens_output == 3000
        assert agent.estimated_cost_usd == 0.05

    @pytest.mark.asyncio
    async def test_error_exit_code(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        proc = FakeProcess(lines=[], returncode=1)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.status == "error"
        assert "exited with code 1" in (agent.error_message or "")

    @pytest.mark.asyncio
    async def test_malformed_json_line_skipped(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        lines = [
            b"not valid json\n",
            json.dumps({"type": "result", "cost_usd": 0.01, "duration_ms": 100, "is_error": False, "num_turns": 1}).encode() + b"\n",
        ]
        proc = FakeProcess(lines=lines, returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        # Should still process the valid event
        assert agent.estimated_cost_usd == 0.01
        assert agent.status == "complete"

    @pytest.mark.asyncio
    async def test_multiple_events(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        events = [
            json.dumps({"type": "system", "session_id": "s1"}).encode() + b"\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Working..."}]}}).encode() + b"\n",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}]}}).encode() + b"\n",
            json.dumps({"type": "result", "cost_usd": 0.02, "duration_ms": 3000, "is_error": False, "num_turns": 2}).encode() + b"\n",
        ]
        proc = FakeProcess(lines=events, returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent.session_id == "s1"
        assert agent.estimated_cost_usd == 0.02
        assert "/a.py" in agent._files_touched_set
        assert agent.status == "complete"

    @pytest.mark.asyncio
    async def test_first_output_tracking(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        agent._spawn_time = time.monotonic() - 2.0  # Simulate 2s ago
        manager.agents["s001"] = agent

        event = json.dumps({"type": "system", "session_id": "s1"}).encode() + b"\n"
        proc = FakeProcess(lines=[event], returncode=0)
        agent._subprocess_proc = proc

        await manager._read_stream_json("s001")

        assert agent._first_output_received is True
        assert agent.time_to_first_output > 0

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        # No agent in manager.agents — should return immediately
        await manager._read_stream_json("nonexistent")


# ── Spawn Validation Tests ──

class TestSpawnOutputModeValidation:
    """Test spawn() validation for output_mode parameter."""

    @pytest.mark.asyncio
    async def test_invalid_output_mode_rejected(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        with pytest.raises(ValueError, match="Invalid output_mode"):
            await manager.spawn(task="test", output_mode="invalid")

    @pytest.mark.asyncio
    async def test_stream_json_non_claude_rejected(self):
        config = Config()
        config.demo_mode = True
        config.spawn_pressure_block = False
        manager = AgentManager(config)
        with pytest.raises(ValueError, match="only supported for the claude-code backend"):
            await manager.spawn(task="test", backend="codex", output_mode="stream-json")

    @pytest.mark.asyncio
    async def test_tmux_mode_default(self):
        config = Config()
        config.demo_mode = True
        config.spawn_pressure_block = False
        manager = AgentManager(config)
        agent = await manager.spawn(task="test")
        assert agent.output_mode == "tmux"


# ── Send Message Tests ──

class TestSendMessageStreamJson:
    """Test send_message rejection for stream-json agents."""

    @pytest.mark.asyncio
    async def test_send_message_rejected_for_stream_json(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        with pytest.raises(ValueError, match="print mode"):
            await manager.send_message("s001", "hello")


# ── Capture Output Tests ──

class TestCaptureOutputStreamJson:
    """Test capture_output skips stream-json agents."""

    @pytest.mark.asyncio
    async def test_capture_output_returns_empty_for_stream_json(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        manager.agents["s001"] = agent

        result = await manager.capture_output("s001")
        assert result == []


# ── Kill Tests ──

class TestKillStreamJsonAgent:
    """Test kill() for subprocess agents."""

    @pytest.mark.asyncio
    async def test_kill_terminates_subprocess(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        proc = FakeProcess(returncode=0)
        agent._subprocess_proc = proc
        manager.agents["s001"] = agent

        result = await manager.kill("s001")

        assert result is True
        assert proc._terminated is True
        assert "s001" not in manager.agents

    @pytest.mark.asyncio
    async def test_kill_cancels_reader_task(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        proc = FakeProcess(returncode=0)
        agent._subprocess_proc = proc

        # Create a fake task
        async def never_finish():
            await asyncio.sleep(999)

        agent._reader_task = asyncio.create_task(never_finish())
        manager.agents["s001"] = agent

        result = await manager.kill("s001")

        assert result is True
        assert agent._reader_task.cancelled() or agent._reader_task.done()


# ── Pause/Resume Tests ──

class TestPauseResumeStreamJson:
    """Test pause/resume for subprocess agents."""

    @pytest.mark.asyncio
    async def test_pause_sends_sigtstp(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent()
        proc = FakeProcess(returncode=0)
        agent._subprocess_proc = proc
        manager.agents["s001"] = agent

        with patch("os.kill") as mock_kill:
            result = await manager.pause("s001")

        assert result is True
        assert agent.status == "paused"
        import signal
        mock_kill.assert_called_once_with(proc.pid, signal.SIGTSTP)

    @pytest.mark.asyncio
    async def test_resume_sends_sigcont(self):
        config = Config()
        config.demo_mode = True
        manager = AgentManager(config)
        agent = make_stream_agent(status="paused")
        proc = FakeProcess(returncode=0)
        agent._subprocess_proc = proc
        manager.agents["s001"] = agent

        with patch("os.kill") as mock_kill:
            result = await manager.resume("s001")

        assert result is True
        assert agent.status == "working"
        import signal
        mock_kill.assert_called_once_with(proc.pid, signal.SIGCONT)


# ── Server API Tests ──

class TestSpawnAPIOutputMode:
    """Test spawn API endpoint accepts output_mode."""

    @pytest.mark.asyncio
    async def test_spawn_with_stream_json(self, aiohttp_client):
        app = make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test task",
            "output_mode": "stream-json",
        })
        data = await resp.json()
        assert resp.status == 201
        assert data.get("output_mode") == "stream-json"

    @pytest.mark.asyncio
    async def test_spawn_invalid_output_mode(self, aiohttp_client):
        app = make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test task",
            "output_mode": "invalid",
        })
        assert resp.status == 400
        data = await resp.json()
        assert "output_mode" in data["error"]

    @pytest.mark.asyncio
    async def test_spawn_stream_json_non_claude_rejected(self, aiohttp_client):
        app = make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test task",
            "backend": "codex",
            "output_mode": "stream-json",
        })
        assert resp.status == 400
        data = await resp.json()
        assert "claude-code" in data["error"]

    @pytest.mark.asyncio
    async def test_spawn_default_tmux(self, aiohttp_client):
        app = make_test_app()
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "task": "test task",
        })
        data = await resp.json()
        assert resp.status == 201
        assert data.get("output_mode") == "tmux"


class TestSendAPIStreamJson:
    """Test send API rejects stream-json agents."""

    @pytest.mark.asyncio
    async def test_send_to_stream_json_agent_returns_400(self, aiohttp_client):
        app = make_test_app()
        client = await aiohttp_client(app)

        # Spawn a stream-json agent
        resp = await client.post("/api/agents", json={
            "task": "test task",
            "output_mode": "stream-json",
        })
        data = await resp.json()
        agent_id = data["id"]

        # Try to send a message
        resp = await client.post(f"/api/agents/{agent_id}/send", json={
            "message": "hello",
        })
        assert resp.status == 400
        data = await resp.json()
        assert "print mode" in data["error"]


# ── BackendConfig Tests ──

class TestBackendConfigOutputMode:
    """Test BackendConfig output_mode field."""

    def test_default_output_mode(self):
        bc = BackendConfig(command="test")
        assert bc.output_mode == "tmux"

    def test_to_dict_includes_output_mode(self):
        bc = BackendConfig(command="test", output_mode="stream-json")
        d = bc.to_dict()
        assert d["output_mode"] == "stream-json"

    def test_known_backends_default_to_tmux(self):
        for name, bc in KNOWN_BACKENDS.items():
            assert bc.output_mode == "tmux", f"Backend {name} should default to tmux"
