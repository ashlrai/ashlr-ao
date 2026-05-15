"""
Ashlr AO — Intelligence Layer

OutputIntelligenceParser (regex-based structured output parsing),
IntelligenceClient (xAI Grok API), phase detection, progress estimation,
and health scoring.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp

from ashlr_ao.constants import _strip_ansi
from ashlr_ao.models import (
    Agent,
    AgentInsight,
    AgentTestResult,
    FileOperation,
    GitOperation,
    ParsedIntent,
    ToolInvocation,
)

if TYPE_CHECKING:
    from ashlr_ao.config import Config

log = logging.getLogger("ashlr")


# ─────────────────────────────────────────────
# Structured Output Intelligence Parser
# ─────────────────────────────────────────────

class OutputIntelligenceParser:
    """Pure regex parser for structured output from Claude Code and other agent backends.
    Runs in the capture loop — ~0.1ms per agent, non-blocking.
    """

    # Tool invocation patterns (Claude Code format)
    _TOOL_PATTERNS = [
        (re.compile(r'Read\("([^"]+)"\)'), "Read"),
        (re.compile(r'Edit\("([^"]+)"\)'), "Edit"),
        (re.compile(r'Write\("([^"]+)"\)'), "Write"),
        (re.compile(r'Bash\("([^"]{0,200})'), "Bash"),
        (re.compile(r'Glob\("([^"]+)"\)'), "Glob"),
        (re.compile(r'Grep\("([^"]+)"\)'), "Grep"),
        (re.compile(r'LS\("([^"]+)"\)'), "LS"),
        (re.compile(r'Task\("([^"]{0,100})'), "Task"),
        (re.compile(r'WebFetch\("([^"]+)"\)'), "WebFetch"),
        (re.compile(r'WebSearch\("([^"]+)"\)'), "WebSearch"),
        (re.compile(r'NotebookEdit\("([^"]+)"\)'), "NotebookEdit"),
        (re.compile(r'Skill\("([^"]+)"'), "Skill"),
        (re.compile(r'Agent\("([^"]{0,100})'), "Agent"),
        (re.compile(r'(mcp__[a-z0-9_-]+__\w+)\('), "MCP"),
        (re.compile(r'TodoWrite\("([^"]{0,100})'), "TodoWrite"),
        (re.compile(r'AskUser\("([^"]{0,100})'), "AskUser"),
    ]

    # Tool result patterns
    _RESULT_SUCCESS = re.compile(r'(?:Tool Result|Result|✓|✔|Success|Updated|Created|Written)')
    _RESULT_ERROR = re.compile(r'(?:Error|Failed|✕|✗|Exception|FAILED)')

    # Git operation patterns
    _GIT_COMMIT = re.compile(r'git commit.*-m\s+["\'](.{0,120})')
    _GIT_CHECKOUT = re.compile(r'git checkout\s+(\S+)')
    _GIT_BRANCH = re.compile(r'git (?:branch|switch)\s+(\S+)')
    _GIT_PUSH = re.compile(r'git push\s+(\S+)')
    _GIT_MERGE = re.compile(r'git merge\s+(\S+)')
    _GIT_SWITCHED = re.compile(r"Switched to (?:a new )?branch '([^']+)'")
    _COMMIT_SHA = re.compile(r'\b([0-9a-f]{7,40})\b.*(?:commit|created|pushed)')

    # Test result patterns
    _PYTEST = re.compile(r'(\d+)\s+passed(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+skipped)?')
    _JEST = re.compile(r'Tests:\s+(?:(\d+)\s+passed)?(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+total)?')
    _MOCHA = re.compile(r'(\d+)\s+passing.*?(?:(\d+)\s+failing)?')
    _COVERAGE = re.compile(r'(?:coverage|Coverage|TOTAL).*?(\d{1,3}(?:\.\d+)?)%')

    # File operation patterns (from natural language output)
    _FILE_READ = re.compile(r'(?:Reading|Scanning|Analyzing)\s+[`"]?([^\s`"]+\.\w{1,8})[`"]?')
    _FILE_WRITE = re.compile(r'(?:Writing|Creating|Updating|Editing)\s+[`"]?([^\s`"]+\.\w{1,8})[`"]?')

    def parse_incremental(self, agent: Agent) -> dict:
        """Parse new output lines since last call. Returns counts of new items parsed."""
        lines = list(agent.output_lines)
        deque_len = len(lines)
        total_added = agent._total_lines_added
        # How many lines have been evicted from the front of the deque?
        evicted = max(0, total_added - deque_len)
        # Convert absolute watermark to deque-relative index
        deque_start = max(0, agent._last_parse_index - evicted)
        if deque_start >= deque_len:
            return {"tools": 0, "files": 0, "git": 0, "tests": 0}

        new_lines = lines[deque_start:]
        agent._last_parse_index = total_added  # absolute position
        now = time.monotonic()

        counts = {"tools": 0, "files": 0, "git": 0, "tests": 0}

        for i, line in enumerate(new_lines):
            line_idx = deque_start + i
            stripped = _strip_ansi(line) if callable(_strip_ansi) else line

            # Tool invocations
            for pattern, tool_name in self._TOOL_PATTERNS:
                m = pattern.search(stripped)
                if m:
                    inv = ToolInvocation(
                        agent_id=agent.id,
                        tool=tool_name,
                        args=m.group(1),
                        timestamp=now,
                        line_index=line_idx,
                    )
                    agent._tool_invocations.append(inv)
                    counts["tools"] += 1

                    # Also record file operations for Read/Edit/Write
                    if tool_name in ("Read", "Edit", "Write", "Glob", "LS"):
                        op_type = "read" if tool_name in ("Read", "Glob", "LS") else "edit" if tool_name == "Edit" else "write"
                        fop = FileOperation(
                            agent_id=agent.id,
                            file_path=m.group(1),
                            operation=op_type,
                            timestamp=now,
                            tool=tool_name,
                        )
                        agent._file_operations.append(fop)
                        counts["files"] += 1
                    break  # One tool per line

            # Tool results — update last invocation's result_status
            if agent._tool_invocations:
                if self._RESULT_SUCCESS.search(stripped):
                    agent._tool_invocations[-1].result_status = "success"
                    agent._tool_invocations[-1].result_snippet = stripped[:100]
                elif self._RESULT_ERROR.search(stripped):
                    agent._tool_invocations[-1].result_status = "error"
                    agent._tool_invocations[-1].result_snippet = stripped[:100]

            # Git operations
            for git_re, git_op in [
                (self._GIT_COMMIT, "commit"),
                (self._GIT_CHECKOUT, "checkout"),
                (self._GIT_BRANCH, "branch"),
                (self._GIT_PUSH, "push"),
                (self._GIT_MERGE, "merge"),
            ]:
                m = git_re.search(stripped)
                if m:
                    detail = m.group(1)
                    gop = GitOperation(
                        agent_id=agent.id,
                        operation=git_op,
                        detail=detail,
                        timestamp=now,
                    )
                    agent._git_operations.append(gop)
                    counts["git"] += 1
                    # Track branch changes (checkout/switch to a branch, not file restores)
                    if git_op in ("checkout", "branch") and not detail.startswith("-") and not detail.startswith("."):
                        agent.git_branch = detail
                    break

            # Also detect git's "Switched to branch" response output
            sw = self._GIT_SWITCHED.search(stripped)
            if sw:
                agent.git_branch = sw.group(1)

            # Test results
            for test_re, framework in [
                (self._PYTEST, "pytest"),
                (self._JEST, "jest"),
                (self._MOCHA, "mocha"),
            ]:
                m = test_re.search(stripped)
                if m:
                    groups = m.groups()
                    passed = int(groups[0] or 0)
                    failed = int(groups[1] or 0) if len(groups) > 1 else 0
                    skipped = int(groups[2] or 0) if len(groups) > 2 else 0
                    tr = AgentTestResult(
                        agent_id=agent.id,
                        passed=passed,
                        failed=failed,
                        skipped=skipped,
                        total=passed + failed + skipped,
                        framework=framework,
                        timestamp=now,
                    )
                    # Check for coverage on same or nearby lines
                    cov_m = self._COVERAGE.search(stripped)
                    if cov_m:
                        tr.coverage_pct = float(cov_m.group(1))
                    agent._test_results.append(tr)
                    counts["tests"] += 1
                    break

            # Natural language file operations
            for fre, fop_type in [
                (self._FILE_READ, "read"),
                (self._FILE_WRITE, "write"),
            ]:
                m = fre.search(stripped)
                if m:
                    fop = FileOperation(
                        agent_id=agent.id,
                        file_path=m.group(1),
                        operation=fop_type,
                        timestamp=now,
                    )
                    agent._file_operations.append(fop)
                    counts["files"] += 1
                    break

        return counts

    def get_activity_summary(self, agent: Agent) -> dict:
        """Get structured activity summary for an agent."""
        # Compute performance timing from tool invocations
        timing = self._compute_timing(agent._tool_invocations)
        return {
            "tool_invocations": [t.to_dict() for t in list(agent._tool_invocations)[-50:]],
            "file_operations": [f.to_dict() for f in list(agent._file_operations)[-50:]],
            "git_operations": [g.to_dict() for g in list(agent._git_operations)[-20:]],
            "test_results": [t.to_dict() for t in list(agent._test_results)[-10:]],
            "summary": {
                "total_tools": len(agent._tool_invocations),
                "total_files": len(agent._file_operations),
                "total_git_ops": len(agent._git_operations),
                "total_test_runs": len(agent._test_results),
                "unique_files": len(set(f.file_path for f in agent._file_operations)),
                "tools_by_type": self._count_by_field(agent._tool_invocations, "tool"),
                "files_by_operation": self._count_by_field(agent._file_operations, "operation"),
            },
            "timing": timing,
        }

    @staticmethod
    def _compute_timing(invocations: collections.deque) -> dict:
        """Compute performance timing stats from tool invocations."""
        if len(invocations) < 2:
            return {"intervals": [], "avg_interval_sec": 0, "longest_gap_sec": 0, "tools_per_min": 0}
        items = list(invocations)
        intervals: list[float] = []
        for i in range(1, len(items)):
            gap = items[i].timestamp - items[i - 1].timestamp
            if gap > 0:
                intervals.append(round(gap, 2))
        if not intervals:
            return {"intervals": [], "avg_interval_sec": 0, "longest_gap_sec": 0, "tools_per_min": 0}
        avg_interval = round(sum(intervals) / len(intervals), 2)
        longest_gap = round(max(intervals), 2)
        # Tools per minute over last 5 minutes
        now = time.monotonic()
        recent = [t for t in items if now - t.timestamp <= 300]
        window = min(300.0, now - items[0].timestamp) if items else 300.0
        tools_per_min = round((len(recent) / max(window, 1.0)) * 60.0, 1) if recent else 0
        # Slowest tool types (avg interval by tool)
        tool_gaps: dict[str, list[float]] = {}
        for i in range(1, len(items)):
            gap = items[i].timestamp - items[i - 1].timestamp
            tool = items[i].tool
            if gap > 0:
                tool_gaps.setdefault(tool, []).append(gap)
        slowest_tools = {k: round(sum(v) / len(v), 2) for k, v in tool_gaps.items() if v}
        return {
            "avg_interval_sec": avg_interval,
            "longest_gap_sec": longest_gap,
            "tools_per_min": tools_per_min,
            "recent_intervals": intervals[-20:],  # Last 20 gaps for sparkline
            "avg_by_tool": slowest_tools,
        }

    @staticmethod
    def _count_by_field(items: list, field_name: str) -> dict:
        counts: dict[str, int] = {}
        for item in items:
            key = getattr(item, field_name, "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts


# Singleton parser instance
_intelligence_parser = OutputIntelligenceParser()

# Pattern alerting throttle: {f"{agent_id}:{label}": last_alert_monotonic_time}
_alert_throttle: dict[str, float] = {}


# ─────────────────────────────────────────────
# Unified Intelligence Client (xAI Grok)
# ─────────────────────────────────────────────

class IntelligenceClient:
    """Unified LLM client for summaries, command parsing, and fleet analysis.
    Uses xAI's OpenAI-compatible API (grok-4.3).
    Replaces both LLMSummarizer and AnthropicIntelligenceClient.
    """

    def __init__(self, config: Config):
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._failures: int = 0
        self._max_failures: int = 5
        self._circuit_reset_time: float = 0.0
        self.available: bool = bool(config.llm_enabled and config.llm_api_key)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _check_circuit(self) -> bool:
        """Returns True if we can make a request."""
        if not self.available:
            return False
        if self._failures >= self._max_failures:
            if time.monotonic() < self._circuit_reset_time:
                return False
            self._failures = 0
        return True

    async def _call(self, messages: list[dict], max_tokens: int = 200,
                    temperature: float = 0.3) -> str | None:
        """Make an OpenAI-compatible API call. Returns response text or None."""
        if not self._check_circuit():
            return None

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.config.llm_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.llm_model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices") or []
                    content = (choices[0].get("message", {}).get("content", "").strip() if choices else "")
                    if content:
                        self._failures = 0
                        return content
                    return None
                elif resp.status in (401, 403):
                    log.error(f"Intelligence disabled: auth failed (HTTP {resp.status}). Check XAI_API_KEY.")
                    self.available = False
                    return None
                elif resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    cooldown = float(retry_after) if retry_after and retry_after.replace('.', '').isdigit() else 60.0
                    self._failures += 1
                    log.warning(f"Intelligence rate limited, cooling down for {cooldown}s (failures: {self._failures}/{self._max_failures})")
                    self._circuit_reset_time = time.monotonic() + cooldown
                    return None
                else:
                    log.debug(f"Intelligence API returned {resp.status}")
                    self._failures += 1
        except asyncio.TimeoutError:
            log.debug("Intelligence API request timed out")
            self._failures += 1
        except Exception as e:
            log.debug(f"Intelligence API error: {e}")
            self._failures += 1

        if self._failures >= self._max_failures:
            self._circuit_reset_time = time.monotonic() + 60.0
            log.warning("Intelligence circuit breaker tripped, cooling down for 60s")

        return None

    async def summarize(self, output_lines: list[str], task: str, role: str, status: str) -> str | None:
        """Generate a 1-line summary from agent output."""
        recent = output_lines[-self.config.llm_max_output_lines:]
        if not recent:
            return None
        output_text = _strip_ansi("\n".join(recent))[:4000]

        result = await self._call(
            messages=[
                {"role": "system", "content": "You are summarizing an AI coding agent's terminal output. Write a concise 1-sentence summary (max 80 chars). Focus on the specific action and file/component. No quotes."},
                {"role": "user", "content": (
                    f"Agent role: {role}\nStatus: {status}\nTask: {task}\n\n"
                    f"Recent output:\n```\n{output_text}\n```"
                )},
            ],
            max_tokens=60,
        )
        return result[:100] if result else None

    async def parse_command(self, transcript: str, agents: list, context: dict) -> ParsedIntent:
        """Parse a natural language command into a structured intent."""
        agent_list = "\n".join(
            f"- {a.name} (id={a.id}, role={a.role}, status={a.status})"
            for a in agents
        ) or "(no agents running)"

        response = await self._call(
            messages=[
                {"role": "system", "content": (
                    "You are a command parser for an AI agent orchestration platform called Ashlr.\n"
                    "Parse the user's natural language command into a JSON intent.\n\n"
                    f"Current agents:\n{agent_list}\n\n"
                    "Respond with ONLY a JSON object:\n"
                    '{"action":"spawn|kill|pause|resume|send|status|query",'
                    '"targets":["agent_id1"],'
                    '"filter":"optional role/status filter",'
                    '"message":"message to send if action=send",'
                    '"parameters":{"role":"general","task":"optional task description"},'
                    '"confidence":0.0-1.0}'
                )},
                {"role": "user", "content": transcript},
            ],
            max_tokens=300,
        )

        if response:
            try:
                data = json.loads(response)
                return ParsedIntent(
                    action=data.get("action", "unknown"),
                    targets=data.get("targets", []),
                    filter=data.get("filter", ""),
                    message=data.get("message", ""),
                    parameters=data.get("parameters", {}),
                    confidence=float(data.get("confidence", 0.5)),
                )
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.debug(f"Failed to parse command response: {e}")

        return ParsedIntent(action="unknown", message=transcript, confidence=0.0)

    async def analyze_fleet(self, agents: list, insights_history: list) -> list[AgentInsight]:
        """Meta-agent analysis: detect conflicts, stuck agents, handoff opportunities."""
        if not agents or len(agents) < 2:
            return []

        agent_summaries = []
        for a in agents:
            files = list(set(f.file_path for f in list(a._file_operations)[-20:]))
            tools = list(set(t.tool for t in list(a._tool_invocations)[-20:]))
            agent_summaries.append(
                f"- {a.name} ({a.role}, {a.status}): {a.summary or a.task}\n"
                f"  Files: {', '.join(files[:10]) or 'none'}\n"
                f"  Recent tools: {', '.join(tools) or 'none'}\n"
                f"  Health: {a.health_score:.0%}, context: {a.context_pct:.0%}"
            )

        response = await self._call(
            messages=[
                {"role": "system", "content": "You analyze AI agent fleets for an orchestration platform. Identify conflicts, stuck agents, handoff opportunities, and anomalies."},
                {"role": "user", "content": (
                    "Analyze these agents and identify issues:\n\n"
                    + "\n".join(agent_summaries)
                    + "\n\nRespond with a JSON array of insights:\n"
                    '[{"type":"conflict|stuck|handoff|anomaly|suggestion",'
                    '"severity":"info|warning|critical",'
                    '"message":"description",'
                    '"agent_ids":["id1"],'
                    '"suggested_action":"what to do"}]\n'
                    "Only include genuine issues. Return [] if everything looks fine."
                )},
            ],
            max_tokens=500,
        )

        insights = []
        if response:
            try:
                data = json.loads(response)
                if isinstance(data, list):
                    for item in data[:10]:
                        insights.append(AgentInsight(
                            id=uuid.uuid4().hex[:8],
                            insight_type=item.get("type", "suggestion"),
                            severity=item.get("severity", "info"),
                            message=item.get("message", ""),
                            agent_ids=item.get("agent_ids", []),
                            suggested_action=item.get("suggested_action", ""),
                            created_at=time.monotonic(),
                        ))
            except (json.JSONDecodeError, KeyError) as e:
                log.debug(f"Failed to parse fleet analysis response: {e}")

        return insights


# ─────────────────────────────────────────────
# Phase Detection & Progress Estimation
# ─────────────────────────────────────────────

PHASE_PATTERNS = {
    "planning": [
        re.compile(r"(?i)(plan|approach|strategy|think|analyze|consider)"),
    ],
    "reading": [
        re.compile(r"(?i)(reading|scanning|exploring|listing) "),
    ],
    "implementing": [
        re.compile(r"(?i)(writing|creating|editing|modifying|adding|removing)"),
        re.compile(r"(?i)(implementing|building|refactoring)"),
    ],
    "testing": [
        re.compile(r"(?i)(running tests|test suite|coverage|verif)"),
        re.compile(r"(?i)(checking|linting|validating)"),
    ],
    "complete": [
        re.compile(r"(?i)\b(done|complete|finished|successfully)\b"),
    ],
}

PHASE_PROGRESS = {
    "unknown": 0.0,
    "planning": 0.10,
    "reading": 0.25,
    "implementing": 0.50,
    "testing": 0.80,
    "complete": 1.0,
}


def detect_phase(lines: list[str]) -> str:
    """Detect the current work phase from recent output."""
    recent = lines[-15:]
    text = "\n".join(recent)

    # Check from most advanced phase backwards
    for phase in ("complete", "testing", "implementing", "reading", "planning"):
        for pat in PHASE_PATTERNS[phase]:
            if pat.search(text):
                return phase
    return "unknown"


def estimate_progress(agent: Agent) -> float:
    """Estimate agent progress as 0.0–1.0."""
    phase = detect_phase(list(agent.output_lines))
    agent._phase = phase
    agent.phase = phase
    base = PHASE_PROGRESS.get(phase, 0.0)

    # Add time-based interpolation within the phase
    if agent.created_at:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(agent.created_at)).total_seconds()
            # Agents typically run 60–300s; add small time bonus
            time_bonus = min(0.10, elapsed / 600)
            return min(1.0, base + time_bonus)
        except Exception as e:
            log.debug(f"Context time bonus calculation failed: {e}")
    return base


def calculate_health_score(agent: Agent, memory_limit_mb: int = 2048) -> float:
    """Calculate composite health score (0.0–1.0) from uptime, errors, output rate, memory.

    Components:
      - uptime_factor: ramps up to 1.0 over 10 minutes (longer uptime = healthier)
      - error_factor: fewer errors = better (1.0 at 0 errors, decays toward 0.2)
      - output_factor: some output = good, no output for >60s = concerning
      - memory_factor: under limit = 1.0, degrades linearly above 75% of limit
    """
    now = time.monotonic()

    # Uptime factor: ramp from 0.5 to 1.0 over 600s (10 min)
    if agent._spawn_time != 0.0:
        uptime_s = now - agent._spawn_time
        uptime_factor = min(1.0, 0.5 + (uptime_s / 1200))  # 0.5 base, full at 10min
    else:
        uptime_factor = 0.5

    # Error factor: exponential decay based on error count
    # 0 errors → 1.0, 5 errors → ~0.6, 10+ errors → ~0.35
    error_factor = max(0.2, 1.0 / (1.0 + agent.error_count * 0.15))

    # Output factor: penalize stale agents (no output for >60s)
    if agent.last_output_time != 0.0:
        silence_s = now - agent.last_output_time
        if silence_s < 30:
            output_factor = 1.0
        elif silence_s < 120:
            output_factor = max(0.3, 1.0 - (silence_s - 30) / 180)
        else:
            output_factor = 0.3
    elif agent._spawn_time != 0.0 and (now - agent._spawn_time) > 30:
        # Never received output but agent has been alive >30s
        output_factor = 0.4
    else:
        output_factor = 0.8  # just spawned, no output yet is fine

    # Memory factor: 1.0 under 75% of limit, linear decay above
    if memory_limit_mb > 0 and agent.memory_mb > 0:
        mem_ratio = agent.memory_mb / memory_limit_mb
        if mem_ratio < 0.75:
            memory_factor = 1.0
        else:
            memory_factor = max(0.1, 1.0 - (mem_ratio - 0.75) * 4.0)
    else:
        memory_factor = 1.0

    # Error/paused status override
    if agent.status == "error":
        return max(0.05, error_factor * 0.3)
    if agent.status == "paused":
        return 0.5  # neutral — paused by user, not unhealthy

    # Weighted composite
    score = (
        uptime_factor * 0.20 +
        error_factor * 0.35 +
        output_factor * 0.25 +
        memory_factor * 0.20
    )
    return round(min(1.0, max(0.0, score)), 3)
