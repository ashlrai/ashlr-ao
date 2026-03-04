"""Tests for ashlr_ao/constants.py — logging, banner, and dependency checks."""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ashlr_ao.constants import (
    setup_logging,
    ColoredFormatter,
    print_banner,
    check_dependencies,
    LOG_COLORS,
    RESET,
)


# ─────────────────────────────────────────────
# setup_logging
# ─────────────────────────────────────────────

class TestSetupLogging:
    def test_sets_root_logger_level(self):
        """setup_logging('DEBUG') sets root logger to DEBUG."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            setup_logging("DEBUG")
            assert root.level == logging.DEBUG
        finally:
            root.setLevel(original_level)
            # Remove handlers we added
            for h in root.handlers:
                if h not in original_handlers:
                    root.removeHandler(h)

    def test_invalid_level_falls_back_to_info(self):
        """setup_logging('BOGUS') falls back to INFO."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            setup_logging("BOGUS")
            assert root.level == logging.INFO
        finally:
            root.setLevel(original_level)
            for h in root.handlers:
                if h not in original_handlers:
                    root.removeHandler(h)

    def test_adds_console_handler(self):
        """setup_logging adds a StreamHandler to root logger."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            setup_logging("INFO")
            new_handlers = [h for h in root.handlers if h not in original_handlers]
            stream_handlers = [h for h in new_handlers if isinstance(h, logging.StreamHandler)
                               and not isinstance(h, logging.handlers.RotatingFileHandler)]
            assert len(stream_handlers) >= 1
        finally:
            for h in root.handlers:
                if h not in original_handlers:
                    root.removeHandler(h)

    def test_adds_file_handler(self):
        """setup_logging adds a RotatingFileHandler to root logger."""
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            setup_logging("INFO")
            new_handlers = [h for h in root.handlers if h not in original_handlers]
            file_handlers = [h for h in new_handlers
                             if isinstance(h, logging.handlers.RotatingFileHandler)]
            assert len(file_handlers) >= 1
        finally:
            for h in root.handlers:
                if h not in original_handlers:
                    root.removeHandler(h)
                    h.close()


# ─────────────────────────────────────────────
# ColoredFormatter
# ─────────────────────────────────────────────

class TestColoredFormatter:
    def test_info_level_has_ansi(self):
        """INFO-level log records get green ANSI coloring."""
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        result = fmt.format(record)
        assert LOG_COLORS["INFO"] in result
        assert RESET in result
        assert "hello" in result

    def test_unknown_level_no_crash(self):
        """A custom log level without a color entry doesn't crash."""
        fmt = ColoredFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            name="test", level=99, pathname="", lineno=0,
            msg="custom", args=(), exc_info=None,
        )
        result = fmt.format(record)
        assert "custom" in result


# ─────────────────────────────────────────────
# print_banner
# ─────────────────────────────────────────────

class TestPrintBanner:
    def test_version_present(self, capsys):
        """Banner output contains the version string."""
        print_banner()
        captured = capsys.readouterr()
        from ashlr_ao import __version__
        assert __version__ in captured.out

    def test_ashlr_present(self, capsys):
        """Banner output contains 'A S H L R'."""
        print_banner()
        captured = capsys.readouterr()
        assert "A S H L R" in captured.out


# ─────────────────────────────────────────────
# check_dependencies
# ─────────────────────────────────────────────

class TestCheckDependencies:
    def test_no_tmux_exits(self):
        """If tmux is not found, check_dependencies calls sys.exit(1)."""
        with patch("ashlr_ao.constants.shutil.which", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                check_dependencies()
            assert exc_info.value.code == 1

    def test_working_claude_returns_true(self):
        """When claude CLI is found and --version succeeds, returns True."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "claude 1.0.0\n"

        with patch("ashlr_ao.constants.shutil.which", side_effect=lambda x: "/usr/bin/" + x):
            with patch("ashlr_ao.constants.os.environ.get", return_value=None):
                with patch("ashlr_ao.constants.subprocess.run", return_value=mock_result):
                    assert check_dependencies() is True

    def test_claudecode_env_returns_false(self):
        """When CLAUDECODE env var is set, returns False (demo mode)."""
        with patch("ashlr_ao.constants.shutil.which", side_effect=lambda x: "/usr/bin/" + x):
            with patch("ashlr_ao.constants.os.environ.get", side_effect=lambda k: "1" if k == "CLAUDECODE" else None):
                assert check_dependencies() is False

    def test_missing_claude_returns_false(self):
        """When claude is not found (but tmux is), returns False."""
        def which_side_effect(cmd):
            return "/usr/bin/tmux" if cmd == "tmux" else None

        with patch("ashlr_ao.constants.shutil.which", side_effect=which_side_effect):
            with patch("ashlr_ao.constants.os.environ.get", return_value=None):
                assert check_dependencies() is False

    def test_claude_timeout_returns_false(self):
        """When claude --version times out, returns False."""
        import subprocess

        with patch("ashlr_ao.constants.shutil.which", side_effect=lambda x: "/usr/bin/" + x):
            with patch("ashlr_ao.constants.os.environ.get", return_value=None):
                with patch("ashlr_ao.constants.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 5)):
                    assert check_dependencies() is False
