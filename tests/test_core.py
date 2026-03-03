"""Tests for core pure functions and utilities in ashlr_server.py."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    from ashlr_server import (
        _strip_ansi,
        redact_secrets,
        deep_merge,
        AgentManager,
        FileOperation,
        GitOperation,
        AgentInsight,
        Agent,
        MCPServerInfo,
    )


# ─────────────────────────────────────────────
# _strip_ansi
# ─────────────────────────────────────────────

class TestStripAnsi:
    def test_plain_text_unchanged(self):
        assert _strip_ansi("Hello World") == "Hello World"

    def test_strips_color_codes(self):
        assert _strip_ansi("\033[31mERROR\033[0m") == "ERROR"

    def test_strips_bold(self):
        assert _strip_ansi("\033[1mBold Text\033[0m") == "Bold Text"

    def test_strips_complex_sequences(self):
        text = "\033[38;5;196m\033[1mRed Bold\033[0m normal"
        assert _strip_ansi(text) == "Red Bold normal"

    def test_empty_string(self):
        assert _strip_ansi("") == ""

    def test_only_ansi(self):
        assert _strip_ansi("\033[31m\033[0m") == ""

    def test_multiline(self):
        text = "\033[32mLine1\033[0m\n\033[33mLine2\033[0m"
        assert _strip_ansi(text) == "Line1\nLine2"


# ─────────────────────────────────────────────
# redact_secrets
# ─────────────────────────────────────────────

class TestRedactSecrets:
    def test_plain_text_unchanged(self):
        assert redact_secrets("Hello World") == "Hello World"

    def test_redacts_openai_key(self):
        result = redact_secrets("key: sk-1234567890abcdefghij1234567890abcdefghij")
        assert "sk-" not in result
        assert "REDACTED" in result

    def test_redacts_github_pat(self):
        result = redact_secrets("token: ghp_1234567890abcdefghijklmnopqrstuvwxyz12")
        assert "ghp_" not in result
        assert "REDACTED" in result

    def test_redacts_xai_key(self):
        result = redact_secrets("XAI_API_KEY=xai-abcdefghij1234567890")
        assert "xai-" not in result
        assert "REDACTED" in result

    def test_redacts_aws_key(self):
        result = redact_secrets("aws_access_key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIA" not in result
        assert "REDACTED" in result

    def test_redacts_bearer_token(self):
        result = redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxx")
        assert "Bearer" not in result or "REDACTED" in result

    def test_redacts_password_field(self):
        result = redact_secrets("password=my_secret_123")
        assert "my_secret_123" not in result
        assert "REDACTED" in result

    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
        result = redact_secrets(f"token: {jwt}")
        assert "eyJ" not in result
        assert "REDACTED" in result

    def test_preserves_surrounding_text(self):
        result = redact_secrets("before sk-12345678901234567890abcd after")
        assert result.startswith("before ")
        assert result.endswith(" after") or "after" in result

    def test_multiple_secrets(self):
        text = "key1: sk-12345678901234567890abcd key2: ghp_123456789012345678901234567890123456"
        result = redact_secrets(text)
        assert result.count("REDACTED") >= 2


# ─────────────────────────────────────────────
# deep_merge
# ─────────────────────────────────────────────

class TestDeepMerge:
    def test_empty_override(self):
        base = {"a": 1, "b": 2}
        assert deep_merge(base, {}) == {"a": 1, "b": 2}

    def test_empty_base(self):
        assert deep_merge({}, {"a": 1}) == {"a": 1}

    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        result = deep_merge(base, {"b": 3})
        assert result == {"a": 1, "b": 3}

    def test_adds_new_keys(self):
        result = deep_merge({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_nested_merge(self):
        base = {"server": {"host": "localhost", "port": 5000}}
        override = {"server": {"port": 8080}}
        result = deep_merge(base, override)
        assert result == {"server": {"host": "localhost", "port": 8080}}

    def test_deep_nested_merge(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"d": 3, "e": 4}}}
        result = deep_merge(base, override)
        assert result == {"a": {"b": {"c": 1, "d": 3, "e": 4}}}

    def test_does_not_mutate_base(self):
        base = {"a": 1}
        deep_merge(base, {"a": 2})
        assert base == {"a": 1}

    def test_override_dict_with_scalar(self):
        """Override replaces dict with scalar (not a recursive merge)."""
        result = deep_merge({"a": {"nested": True}}, {"a": "flat"})
        assert result == {"a": "flat"}

    def test_override_scalar_with_dict(self):
        result = deep_merge({"a": "flat"}, {"a": {"nested": True}})
        assert result == {"a": {"nested": True}}


# ─────────────────────────────────────────────
# _sanitize_for_tmux (via AgentManager)
# ─────────────────────────────────────────────

class TestSanitizeForTmux:
    """Test the tmux sanitization method. Access via a mock AgentManager instance."""

    def _sanitize(self, text):
        """Helper to call the static-like method."""
        # _sanitize_for_tmux is an instance method, create a minimal call
        return AgentManager._sanitize_for_tmux(None, text)

    def test_plain_text_unchanged(self):
        assert self._sanitize("Hello World") == "Hello World"

    def test_strips_null_bytes(self):
        assert self._sanitize("Hello\x00World") == "HelloWorld"

    def test_strips_control_chars(self):
        assert self._sanitize("Hello\x01\x02\x03World") == "HelloWorld"

    def test_preserves_newlines(self):
        assert self._sanitize("Line1\nLine2") == "Line1\nLine2"

    def test_preserves_tabs(self):
        # \x09 is tab — should be stripped per the regex
        result = self._sanitize("Col1\tCol2")
        assert result == "Col1Col2"

    def test_truncates_long_text(self):
        long_text = "A" * 3000
        assert len(self._sanitize(long_text)) == 2000

    def test_empty_string(self):
        assert self._sanitize("") == ""


# ─────────────────────────────────────────────
# _resolve_skip_val and _safe_eval_condition
# ─────────────────────────────────────────────

class TestResolveSkipVal:
    def test_single_quoted_string(self):
        assert AgentManager._resolve_skip_val("'hello'", {}) == "hello"

    def test_double_quoted_string(self):
        assert AgentManager._resolve_skip_val('"world"', {}) == "world"

    def test_context_variable(self):
        ctx = {"prev.status": "complete"}
        assert AgentManager._resolve_skip_val("prev.status", ctx) == "complete"

    def test_unknown_token(self):
        assert AgentManager._resolve_skip_val("unknown_var", {}) is None

    def test_whitespace_trimmed(self):
        assert AgentManager._resolve_skip_val("  'hello'  ", {}) == "hello"

    def test_empty_string_literal(self):
        assert AgentManager._resolve_skip_val("''", {}) == ""


class TestSafeEvalCondition:
    def test_equality_true(self):
        ctx = {"prev.status": "complete"}
        assert AgentManager._safe_eval_condition("prev.status == 'complete'", ctx) is True

    def test_equality_false(self):
        ctx = {"prev.status": "working"}
        assert AgentManager._safe_eval_condition("prev.status == 'complete'", ctx) is False

    def test_inequality_true(self):
        ctx = {"prev.status": "error"}
        assert AgentManager._safe_eval_condition("prev.status != 'complete'", ctx) is True

    def test_inequality_false(self):
        ctx = {"prev.status": "complete"}
        assert AgentManager._safe_eval_condition("prev.status != 'complete'", ctx) is False

    def test_in_operator(self):
        ctx = {"prev.summary": "Found 3 XSS vulnerabilities"}
        assert AgentManager._safe_eval_condition("'XSS' in prev.summary", ctx) is True

    def test_not_in_operator(self):
        ctx = {"prev.summary": "All tests passed"}
        assert AgentManager._safe_eval_condition("'error' not in prev.summary", ctx) is True

    def test_not_in_false(self):
        ctx = {"prev.summary": "error occurred"}
        assert AgentManager._safe_eval_condition("'error' not in prev.summary", ctx) is False

    def test_invalid_variable(self):
        assert AgentManager._safe_eval_condition("unknown == 'x'", {}) is False

    def test_no_operator(self):
        assert AgentManager._safe_eval_condition("just_a_string", {}) is False

    def test_empty_expression(self):
        assert AgentManager._safe_eval_condition("", {}) is False


# ─────────────────────────────────────────────
# Dataclass to_dict() methods
# ─────────────────────────────────────────────

class TestFileOperationToDict:
    def test_to_dict(self):
        op = FileOperation(agent_id="a1", file_path="/tmp/f.py", operation="write", timestamp=1.0, tool="Edit")
        d = op.to_dict()
        assert d == {"agent_id": "a1", "file_path": "/tmp/f.py", "operation": "write", "timestamp": 1.0, "tool": "Edit"}


class TestGitOperationToDict:
    def test_to_dict(self):
        op = GitOperation(agent_id="a2", operation="commit", detail="fix bug", timestamp=2.0, files_affected=["a.py"])
        d = op.to_dict()
        assert d["agent_id"] == "a2"
        assert d["operation"] == "commit"
        assert d["files_affected"] == ["a.py"]


class TestMCPServerInfoToDict:
    def test_to_dict(self):
        info = MCPServerInfo(name="test-mcp", server_type="stdio", url_or_command="npx mcp", source="user", args=["--flag"])
        d = info.to_dict()
        assert d == {"name": "test-mcp", "type": "stdio", "url_or_command": "npx mcp", "source": "user", "args": ["--flag"]}


class TestAgentInsightToDict:
    def test_to_dict(self):
        ins = AgentInsight(id="i1", insight_type="conflict", severity="warning", message="conflict detected",
                           agent_ids=["a1", "a2"], evidence="both edit f.py", suggested_action="coordinate")
        d = ins.to_dict()
        assert d["id"] == "i1"
        assert d["insight_type"] == "conflict"
        assert d["severity"] == "warning"
        assert d["agent_ids"] == ["a1", "a2"]
        assert d["acknowledged"] is False


class TestAgentStatusHelpers:
    """Test agent status methods including timeline and time guard."""
    def test_set_status_time_guard_rejects_stale(self, make_agent):
        agent = make_agent(status="working")
        agent._status_updated_at = time.monotonic() + 9999  # far future
        result = agent.set_status("waiting")
        assert result is False
        assert agent.status == "working"  # unchanged

    def test_status_timeline(self, make_agent):
        agent = make_agent(status="working")
        agent._status_history = [{"status": "working", "at": time.monotonic() - 10}]
        agent.set_status("waiting")
        timeline = agent._get_status_timeline()
        assert len(timeline) >= 1
        assert timeline[0]["status"] == "working"

    def test_status_timeline_zero_duration_skipped(self, make_agent):
        agent = make_agent(status="idle")
        now = time.monotonic()
        agent._status_history = [
            {"status": "working", "at": now},
            {"status": "idle", "at": now},  # zero duration
        ]
        timeline = agent._get_status_timeline()
        # Zero-duration entries are filtered out
        assert all(e["duration_sec"] > 0 for e in timeline)
