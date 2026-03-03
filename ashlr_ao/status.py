"""
Ashlr AO — Agent Status Detection

Terminal output parsing to detect agent status, extract summaries,
and suggest follow-up actions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ashlr_ao.constants import _strip_ansi

if TYPE_CHECKING:
    from ashlr_ao.models import Agent


# ─────────────────────────────────────────────
# Status Detection Patterns
# ─────────────────────────────────────────────

STATUS_PATTERNS = {
    "planning": [
        re.compile(r"(?i)\bplan\b"),
        re.compile(r"(?i)let me (think|analyze|plan|consider)"),
        re.compile(r"(?i)here'?s (my|the) (plan|approach|strategy)"),
        re.compile(r"(?i)I'll (start by|first|begin)"),
        re.compile(r"(?i)thinking about"),
        re.compile(r"<thinking>"),  # Claude thinking blocks
    ],
    "reading": [
        re.compile(r"(?i)(reading|loading|scanning|parsing) .+\.\w+"),
        re.compile(r"(?i)(reading|loading|scanning|parsing) (directory|folder|project|codebase)"),
        re.compile(r"(?i)exploring .+"),
        re.compile(r"(?i)searching (for|in|through|across)"),
        re.compile(r"(?i)(looking at|examining|inspecting|reviewing) .+"),
    ],
    "working": [
        re.compile(r"(?i)(writing|creating|editing|updating) \S+\.\w+"),
        re.compile(r"(?i)(running|executing) .+"),
        re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"),
        re.compile(r"█+░*"),
        re.compile(r"(?i)Tool Use:"),
        re.compile(r"(?i)Bash:"),
        re.compile(r"(?i)files? (created|edited|read)"),
        re.compile(r"(?i)(checking|auditing|analyzing|testing)"),
        # Git operations
        re.compile(r"(?i)(git (add|commit|push|pull|checkout|merge|rebase))"),
        # Build/compile operations
        re.compile(r"(?i)(building|compiling|bundling|webpack|vite|esbuild)"),
        # Test result patterns (working, not complete — tests are still running)
        re.compile(r"(?i)(\d+ (tests?|specs?) (passed|failed|skipped))"),
        # Claude Code tool calls
        re.compile(r"(?i)(Read|Write|Edit|Glob|Grep|Bash)\("),
        re.compile(r"(?i)Tool (Result|Output):"),
        # Package management / network
        re.compile(r"(?i)(installing|downloading|fetching|uploading)"),
        # Linting / formatting (active operations, not result messages)
        re.compile(r"(?i)(linting|formatting|running (lint|prettier|eslint|mypy|ruff))"),
        # Docker / container operations
        re.compile(r"(?i)(docker (build|run|pull|push|compose))"),
        # Database operations
        re.compile(r"(?i)(migrating|seeding|running migration)"),
        # Deploy operations
        re.compile(r"(?i)(deploying|pushing to|releasing)"),
    ],
    "waiting": [
        re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
        re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
        re.compile(r"(?i)proceed\?"),
        re.compile(r"(?i)\bapprove\b"),
        re.compile(r"(?i)please (confirm|respond|reply|answer|choose|select)"),
        re.compile(r"(?i)waiting for (input|response|confirmation|approval)"),
        re.compile(r"(?i)enter (a |your )?(value|name|path|password|token)"),
    ],
    "error": [
        # Fatal patterns — actual crashes, not just mentions of "error" in output
        re.compile(r"(?i)\b(traceback|fatal|panic|segfault|SIGKILL|SIGSEGV)\b"),
        re.compile(r"(?i)unhandled (exception|error|rejection)"),
        re.compile(r"(?i)command not found"),
        re.compile(r"(?i)permission denied"),
        re.compile(r"(?i)(cannot|couldn'?t) (connect|reach|find|open|read|write)"),
        re.compile(r"(?i)out of memory"),
        re.compile(r"(?i)killed by signal"),
    ],
    # Non-fatal error mentions — tracked for health scoring but don't flip status
    "error_mention": [
        re.compile(r"(?i)(?<!\bno\s)\b(error|exception|failed)\b(?!\s*handl)"),
    ],
    "complete": [
        re.compile(r"(?i)\b(done|complete|finished|successfully)\b"),
        re.compile(r"(?i)task completed"),
        re.compile(r"(?i)all tests pass"),
        re.compile(r"(?i)changes committed"),  # Git commit completion
        re.compile(r"(?i)no (issues|errors|warnings) found"),  # Clean lint/test
    ],
}

WAITING_LINE_PATTERNS = [
    re.compile(r"(?i)(do you want|shall I|should I|would you like)"),
    re.compile(r"(?i)(yes/no|y/n|\[Y/n\]|\[y/N\])"),
    re.compile(r"(?i)proceed\?"),
    # Only match trailing ? if the line looks like a direct question to the user
    # (contains "you", "I", or starts with a question word)
    re.compile(r"(?i)^(do|can|will|should|shall|would|may|is|are|does|did|have|has)\b.+\?\s*$"),
]


# ─────────────────────────────────────────────
# Follow-up Suggestions
# ─────────────────────────────────────────────

FOLLOWUP_SUGGESTIONS: dict[str, list[dict]] = {
    "backend": [
        {"role": "tester", "task_template": "Write tests for the changes made by {name}: {summary}",
         "reason": "Backend changes should be tested"},
        {"role": "reviewer", "task_template": "Review the code changes made by {name}: {summary}",
         "reason": "Backend code should be reviewed"},
    ],
    "frontend": [
        {"role": "tester", "task_template": "Test the UI changes made by {name}: {summary}",
         "reason": "Frontend changes need testing"},
        {"role": "reviewer", "task_template": "Review the frontend changes by {name}: {summary}",
         "reason": "Frontend code should be reviewed"},
    ],
    "architect": [
        {"role": "backend", "task_template": "Implement the architecture designed by {name}: {summary}",
         "reason": "Architecture plans need implementation"},
    ],
    "security": [
        {"role": "backend", "task_template": "Fix the security issues found by {name}: {summary}",
         "reason": "Security findings need remediation"},
    ],
    "reviewer": [
        {"role": "backend", "task_template": "Address the review feedback from {name}: {summary}",
         "reason": "Review feedback should be addressed"},
    ],
    "tester": [
        {"role": "backend", "task_template": "Fix the test failures found by {name}: {summary}",
         "reason": "Failed tests need fixes"},
    ],
}


# ─────────────────────────────────────────────
# Status Detection Functions
# ─────────────────────────────────────────────

def _suggest_followup(agent: Agent) -> dict | None:
    """Suggest a follow-up agent based on what this agent did."""
    suggestions = FOLLOWUP_SUGGESTIONS.get(agent.role, [])
    if not suggestions:
        return None
    # Pick the first applicable suggestion
    s = suggestions[0]
    task = s["task_template"].format(name=agent.name, summary=(agent.summary or agent.task or "")[:100])
    return {
        "suggested_role": s["role"],
        "suggested_task": task,
        "reason": s["reason"],
        "source_agent_id": agent.id,
        "source_agent_name": agent.name,
        "message": f'{agent.name} completed — {s["reason"]}. Suggested: spawn {s["role"]} agent.',
    }


def parse_agent_status(recent_lines: list[str], agent: Agent, backend_patterns: dict[str, list[str]] | None = None) -> str:
    """Parse recent terminal output to detect agent status.
    Priority: waiting > error > reading > planning > working > complete > current status.
    Tracks non-fatal error mentions for health scoring without flipping status.
    backend_patterns override defaults for specific status categories."""
    tail_text = "\n".join(recent_lines[-5:])

    # Merge backend-specific patterns with defaults
    effective_patterns: dict[str, list] = {}
    for cat, pats in STATUS_PATTERNS.items():
        effective_patterns[cat] = list(pats)
    if backend_patterns:
        for cat, str_pats in backend_patterns.items():
            if cat not in effective_patterns:
                effective_patterns[cat] = []
            for sp in str_pats:
                try:
                    effective_patterns[cat].append(re.compile(sp))
                except re.error:
                    pass

    # Track non-fatal error mentions for health scoring (don't affect status)
    for pattern in effective_patterns.get("error_mention", []):
        if pattern.search(tail_text):
            agent.error_count = min(agent.error_count + 1, 100)

    # Check for waiting (highest priority, only in recent lines to avoid stale matches)
    for pattern in effective_patterns["waiting"]:
        if pattern.search(tail_text):
            # Extract the question
            agent.needs_input = True
            agent.input_prompt = _extract_question(recent_lines)
            return "waiting"

    # Check last non-empty line for question mark
    last_line = ""
    for line in reversed(recent_lines):
        if line.strip():
            last_line = line.strip()
            break

    for pattern in WAITING_LINE_PATTERNS:
        if pattern.search(last_line):
            agent.needs_input = True
            agent.input_prompt = last_line
            return "waiting"

    # Check for fatal error (only in last 5 lines to avoid old mentions)
    for pattern in effective_patterns["error"]:
        m = pattern.search(tail_text)
        if m:
            agent.needs_input = False
            # Capture the matching line as error context
            for line in reversed(recent_lines[-5:]):
                if pattern.search(line):
                    agent.error_message = _strip_ansi(line.strip())[:200]
                    break
            return "error"

    # Check for reading (before working, more specific)
    for pattern in effective_patterns["reading"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "reading"

    # Check for planning (only in recent lines to avoid matching task descriptions)
    for pattern in effective_patterns["planning"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "planning"

    # Check for working (use tail_text to avoid matching stale output)
    # Must come before "complete" so active-work indicators win over prompt presence
    for pattern in effective_patterns["working"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "working"

    # Check for complete / idle (e.g. CLI prompt visible, no active work)
    for pattern in effective_patterns["complete"]:
        if pattern.search(tail_text):
            agent.needs_input = False
            return "idle"

    # No clear signal — keep current status
    agent.needs_input = False
    return agent.status if agent.status != "spawning" else "working"


def _extract_question(lines: list[str]) -> str:
    """Extract the agent's question from recent output lines."""
    # Look backwards for question-like content
    question_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            if question_lines:
                break
            continue
        question_lines.insert(0, stripped)
        if len(question_lines) >= 3:
            break
    return "\n".join(question_lines) if question_lines else "Agent needs your input"


# ─────────────────────────────────────────────
# Summary Extraction
# ─────────────────────────────────────────────

_FILE_PATH_RE = re.compile(r"(?:(?:src|lib|test|app|pkg)/)?[\w\-./]+\.\w{1,5}")
_TEST_RESULT_RE = re.compile(r"(?i)(\d+)\s*(?:tests?\s+)?pass(?:ed)?.*?(\d+)\s*fail")
_COVERAGE_RE = re.compile(r"(?i)coverage[:\s]+(\d+)%")
_FILES_PROGRESS_RE = re.compile(r"(?i)(\d+)\s*(?:of|/)\s*(\d+)\s*(?:files?|items?)")

_ACTION_PATTERNS = [
    re.compile(r"(?i)(writing|creating|editing|reading|updating) (.+)"),
    re.compile(r"(?i)(running|executing) (.+)"),
    re.compile(r"(?i)(analyzing|reviewing|checking) (.+)"),
    re.compile(r"(?i)(installing|building|compiling) (.+)"),
    re.compile(r"(?i)(scanning|auditing|testing|deploying) (.+)"),
    re.compile(r"(?i)(found \d+.+)"),
    re.compile(r"(?i)(coverage.+\d+%)"),
    re.compile(r"(?i)(committed .+ to .+)"),  # Git commit
]

# Claude Code intent patterns — "I'll fix the auth bug" → extract intent
_INTENT_RE = re.compile(r"(?i)I'll\s+(.{10,80})")
_GIT_COMMIT_RE = re.compile(r"(?i)committed .+ to .+")


def extract_summary(lines: list[str], task: str, status: str = "") -> str:
    """Extract a 1-2 line summary from recent output with file paths and test results."""
    MAX_LEN = 80  # Tighter cap for card display

    # Check for test results
    for line in reversed(lines[-20:]):
        m = _TEST_RESULT_RE.search(line)
        if m:
            return _strip_ansi(f"Tests: {m.group(1)} passed, {m.group(2)} failed")[:MAX_LEN]
        m = _COVERAGE_RE.search(line)
        if m:
            return _strip_ansi(f"Coverage: {m.group(1)}%")[:MAX_LEN]

    # Git commit summaries
    for line in reversed(lines[-20:]):
        m = _GIT_COMMIT_RE.search(_strip_ansi(line.strip()))
        if m:
            return _strip_ansi(m.group(0))[:MAX_LEN]

    # Extract file paths being worked on
    for line in reversed(lines[-20:]):
        stripped = _strip_ansi(line.strip())
        if not stripped:
            continue

        for pattern in _ACTION_PATTERNS:
            match = pattern.search(stripped)
            if match:
                # Try to extract file path from the match
                fp = _FILE_PATH_RE.search(stripped)
                if fp:
                    return f"{match.group(1).title()} {fp.group(0)}"[:MAX_LEN]
                return stripped[:MAX_LEN]

    # Claude Code intent patterns — "I'll fix the authentication bug"
    for line in reversed(lines[-20:]):
        m = _INTENT_RE.search(_strip_ansi(line.strip()))
        if m:
            return m.group(1).strip().rstrip(".")[:MAX_LEN]

    # Files progress
    for line in reversed(lines[-15:]):
        m = _FILES_PROGRESS_RE.search(_strip_ansi(line))
        if m:
            return f"Progress: {m.group(1)} of {m.group(2)} files"[:MAX_LEN]

    # When status is error, show first error line
    if status == "error":
        for line in reversed(lines[-20:]):
            stripped = _strip_ansi(line.strip())
            if stripped and re.search(r"(?i)(error|traceback|fatal|panic)", stripped):
                return stripped[:MAX_LEN]

    # Fallback: last non-empty line (skip TUI chrome from Claude Code etc.)
    _TUI_CHROME = {
        "bypass permissions", "shift+tab to cycle", "Welcome back",
        "Recent activity", "/resume for more", "What's new",
        "/release-notes", "Enter a prompt", "Claude Code v",
    }
    for line in reversed(lines[-10:]):
        stripped = _strip_ansi(line.strip())
        if not stripped or len(stripped) <= 5:
            continue
        # Skip box-drawing lines and TUI chrome
        if stripped[0] in "╭╰│╔╗╚╝║─━┌┐└┘├┤┬┴┼▐▝❯":
            continue
        if any(chrome in stripped for chrome in _TUI_CHROME):
            continue
        return stripped[:MAX_LEN]

    return _strip_ansi(task[:MAX_LEN]) if task else "Working..."
