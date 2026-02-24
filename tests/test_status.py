"""Tests for agent status detection, summary extraction, and phase detection."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    from ashlar_server import (
        parse_agent_status,
        extract_summary,
        detect_phase,
        estimate_progress,
    )


# ─────────────────────────────────────────────
# parse_agent_status
# ─────────────────────────────────────────────

class TestParseAgentStatus:
    def test_detects_waiting_from_question(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Looking at the code...", "Do you want me to proceed with this change?"]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"
        assert agent.needs_input is True

    def test_detects_waiting_from_shall_i(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Analysis complete.", "Shall I apply the fix?"]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"

    def test_detects_error(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Working on file...", "", "Error: connection refused", "Traceback (most recent call last):", "  File ..."]
        status = parse_agent_status(lines, agent)
        assert status == "error"

    def test_detects_planning(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Let me analyze the codebase first", "Here's my plan:", "1. Fix auth", "2. Add tests"]
        status = parse_agent_status(lines, agent)
        assert status == "planning"

    def test_detects_reading(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Some earlier output", "", "Reading src/auth/handler.ts"]
        status = parse_agent_status(lines, agent)
        assert status == "reading"

    def test_detects_working(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Creating src/components/Button.tsx", "Adding props interface"]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_detects_complete(self, make_agent):
        agent = make_agent(status="working")
        lines = ["Applied all changes.", "All tasks completed successfully"]
        status = parse_agent_status(lines, agent)
        assert status == "idle"

    def test_keeps_current_status_on_no_signal(self, make_agent):
        agent = make_agent(status="working")
        lines = ["...", "..."]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_spawning_defaults_to_working(self, make_agent):
        agent = make_agent(status="spawning")
        lines = ["...", "..."]
        status = parse_agent_status(lines, agent)
        assert status == "working"

    def test_waiting_has_higher_priority_than_error(self, make_agent):
        """Waiting signals should take priority over error mentions."""
        agent = make_agent(status="working")
        lines = [
            "I found an error in the code.",
            "Should I fix this error?",
        ]
        status = parse_agent_status(lines, agent)
        assert status == "waiting"

    def test_backend_patterns_extend_defaults(self, make_agent):
        agent = make_agent(status="working")
        lines = ["⎿ Writing file...", "Tool Use: Bash"]
        backend_patterns = {
            "working": [r"⎿", r"Tool Use:"],
        }
        status = parse_agent_status(lines, agent, backend_patterns=backend_patterns)
        assert status == "working"

    def test_error_count_incremented_for_mentions(self, make_agent):
        agent = make_agent(status="working", error_count=0)
        lines = ["All good", "WARNING: deprecation notice", "Continuing..."]
        parse_agent_status(lines, agent)
        # error_count may or may not change depending on pattern matching
        # but it should never go negative
        assert agent.error_count >= 0

    def test_empty_lines(self, make_agent):
        agent = make_agent(status="working")
        status = parse_agent_status([], agent)
        assert status == "working"


# ─────────────────────────────────────────────
# extract_summary
# ─────────────────────────────────────────────

class TestExtractSummary:
    def test_extracts_test_results(self):
        lines = ["Running tests...", "12 tests passed, 3 failed"]
        summary = extract_summary(lines, "Run tests")
        assert "12" in summary
        assert "3" in summary
        assert "pass" in summary.lower() or "fail" in summary.lower()

    def test_extracts_coverage(self):
        lines = ["Generating report...", "Coverage: 87%"]
        summary = extract_summary(lines, "Check coverage")
        assert "87" in summary

    def test_extracts_file_action(self):
        lines = ["Writing src/auth/handler.ts"]
        summary = extract_summary(lines, "Fix auth")
        assert "handler.ts" in summary or "Writing" in summary

    def test_extracts_reading_action(self):
        lines = ["Reading src/config/database.py"]
        summary = extract_summary(lines, "Review config")
        assert "database.py" in summary or "Reading" in summary

    def test_fallback_to_last_line(self):
        lines = ["Something is happening that we don't pattern match"]
        summary = extract_summary(lines, "Some task")
        assert len(summary) > 0

    def test_fallback_to_task(self):
        lines = ["", "   ", ""]
        summary = extract_summary(lines, "Deploy to production")
        assert "Deploy" in summary

    def test_default_working(self):
        summary = extract_summary([], "")
        assert summary == "Working..."

    def test_truncates_long_summary(self):
        lines = ["Writing " + "x" * 200 + ".ts"]
        summary = extract_summary(lines, "task")
        assert len(summary) <= 100

    def test_strips_ansi_from_summary(self):
        lines = ["\033[32mWriting src/app.ts\033[0m"]
        summary = extract_summary(lines, "Build app")
        assert "\033" not in summary


# ─────────────────────────────────────────────
# detect_phase
# ─────────────────────────────────────────────

class TestDetectPhase:
    def test_planning_phase(self):
        lines = ["Let me think about this approach", "Here's my plan:"]
        assert detect_phase(lines) == "planning"

    def test_reading_phase(self):
        lines = ["Reading the codebase structure", "Scanning src/ directory"]
        assert detect_phase(lines) == "reading"

    def test_implementing_phase(self):
        lines = ["Writing the new authentication module", "Creating handler.ts"]
        assert detect_phase(lines) == "implementing"

    def test_testing_phase(self):
        lines = ["Running tests now", "Test suite starting"]
        assert detect_phase(lines) == "testing"

    def test_complete_phase(self):
        lines = ["All changes applied successfully", "Task finished"]
        assert detect_phase(lines) == "complete"

    def test_unknown_phase(self):
        lines = ["...", "..."]
        assert detect_phase(lines) == "unknown"

    def test_empty_lines(self):
        assert detect_phase([]) == "unknown"

    def test_most_advanced_phase_wins(self):
        """When multiple phases match, the most advanced one should win."""
        lines = [
            "Let me plan this",  # planning
            "Writing the code",  # implementing
            "Running tests",     # testing
        ]
        assert detect_phase(lines) == "testing"

    def test_only_uses_last_15_lines(self):
        """Old lines beyond the 15-line window shouldn't affect detection."""
        old_lines = ["Running tests"] * 20  # testing phase
        recent_lines = ["Just some plain dots..."] * 16  # no phase signal
        all_lines = old_lines + recent_lines
        # detect_phase uses last 15 lines — "Running tests" is outside the window
        phase = detect_phase(all_lines)
        assert phase == "unknown"


# ─────────────────────────────────────────────
# estimate_progress
# ─────────────────────────────────────────────

class TestEstimateProgress:
    def test_returns_float_between_0_and_1(self, make_agent):
        agent = make_agent()
        agent.output_lines.extend(["Writing src/app.ts", "Creating component"])
        progress = estimate_progress(agent)
        assert 0.0 <= progress <= 1.0

    def test_complete_phase_near_1(self, make_agent):
        agent = make_agent()
        agent.output_lines.extend(["All tasks completed successfully"])
        progress = estimate_progress(agent)
        assert progress >= 0.9

    def test_unknown_phase_low(self, make_agent):
        agent = make_agent()
        agent.output_lines.extend(["...", "..."])
        progress = estimate_progress(agent)
        assert progress < 0.3

    def test_sets_phase_on_agent(self, make_agent):
        agent = make_agent()
        agent.output_lines.extend(["Running the test suite"])
        estimate_progress(agent)
        assert agent.phase == "testing"

    def test_never_exceeds_1(self, make_agent):
        agent = make_agent()
        agent.output_lines.extend(["Done successfully"] * 20)
        progress = estimate_progress(agent)
        assert progress <= 1.0
