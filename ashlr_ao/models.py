"""
Ashlr AO — Data Models

All dataclasses used across the application: Agent, WorkflowRun, QueuedTask,
SystemMetrics, intelligence models (ToolInvocation, FileOperation, etc.),
and auth models (Organization, User).
"""

import asyncio
import collections
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ashlr_ao.backends import KNOWN_BACKENDS
from ashlr_ao.constants import redact_secrets
from ashlr_ao.roles import BUILTIN_ROLES


# ─────────────────────────────────────────────
# Output Snapshots
# ─────────────────────────────────────────────


@dataclass
class OutputSnapshot:
    """Capture of agent output at a key lifecycle point."""
    id: str
    agent_id: str
    trigger: str  # "error", "waiting", "complete", "manual"
    status: str
    summary: str
    line_count: int
    output_tail: str  # Last N lines of output
    context_pct: float
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "trigger": self.trigger,
            "status": self.status,
            "summary": self.summary,
            "line_count": self.line_count,
            "output_tail": self.output_tail,
            "context_pct": self.context_pct,
            "created_at": self.created_at,
        }


# ─────────────────────────────────────────────
# Auth Models
# ─────────────────────────────────────────────


@dataclass
class Organization:
    """A team/company that shares an Ashlr instance."""
    id: str
    name: str
    slug: str
    created_at: str = ""
    license_key: str = ""
    plan: str = "community"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "slug": self.slug, "created_at": self.created_at, "plan": self.plan}


@dataclass
class User:
    """An authenticated user within an organization."""
    id: str
    email: str
    display_name: str
    password_hash: str
    role: str = "member"  # "admin" | "member"
    org_id: str = ""
    created_at: str = ""
    last_login: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "email": self.email, "display_name": self.display_name,
            "role": self.role, "org_id": self.org_id, "created_at": self.created_at,
            "last_login": self.last_login,
        }


# ─────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────


@dataclass
class Agent:
    id: str
    name: str
    role: str
    status: str  # spawning|planning|reading|working|waiting|idle|error|paused
    working_dir: str
    backend: str
    task: str
    summary: str = ""
    context_pct: float = 0.0
    memory_mb: float = 0.0
    cpu_pct: float = 0.0
    needs_input: bool = False
    input_prompt: str | None = None
    error_message: str | None = None
    project_id: str | None = None
    tmux_session: str = ""
    pid: int | None = None
    created_at: str = ""
    updated_at: str = ""
    script_path: str | None = None
    related_agents: list = field(default_factory=list)
    progress_pct: float = 0.0
    phase: str = ""
    # Auto-restart fields
    restart_count: int = 0
    max_restarts: int = 3
    last_restart_time: float = 0.0
    restarted_at: str = ""
    _restart_in_progress: bool = field(default=False, repr=False)
    _restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Health scoring fields
    health_score: float = 1.0
    error_count: int = 0
    last_output_time: float = 0.0
    # Per-agent metrics
    time_to_first_output: float = 0.0
    total_output_lines: int = 0
    output_rate: float = 0.0
    _phase: str = field(default="unknown", repr=False)
    output_lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=2000))
    _archived_lines: int = field(default=0, repr=False)
    _prev_output_hash: int = field(default=0, repr=False)
    _total_chars: int = field(default=0, repr=False)
    _spawn_time: float = field(default=0.0, repr=False)
    _last_needs_input_event: float = field(default=0.0, repr=False)
    _last_llm_summary_time: float = field(default=0.0, repr=False)
    _llm_summary: str = field(default="", repr=False)
    _summary_output_hash: int = field(default=0, repr=False)
    # Orchestration fields
    model: str | None = None
    tools_allowed: list[str] | None = None
    session_id: str | None = None
    system_prompt: str | None = None
    plan_mode: bool = False
    owner_id: str | None = None
    owner_name: str | None = None
    git_branch: str | None = None
    workflow_run_id: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    estimated_cost_usd: float = 0.0
    files_touched: int = 0
    _files_touched_set: set = field(default_factory=set, repr=False)
    unread_messages: int = field(default=0, repr=False)
    _first_output_received: bool = field(default=False, repr=False)
    _output_line_timestamps: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=60), repr=False
    )
    _error_entered_at: float = field(default=0.0, repr=False)
    _health_low_warned: bool = field(default=False, repr=False)
    _health_critical_warned: bool = field(default=False, repr=False)
    _stale_warned: bool = field(default=False, repr=False)
    _workflow_stall_warned: bool = field(default=False, repr=False)
    _workflow_hung_warned: bool = field(default=False, repr=False)
    _pathological: bool = field(default=False, repr=False)
    _last_working_time: float = field(default=0.0, repr=False)
    _context_auto_paused: bool = field(default=False, repr=False)
    _context_exhaustion_warned: bool = field(default=False, repr=False)
    _budget_warned: bool = field(default=False, repr=False)
    _memory_pause_warned: bool = field(default=False, repr=False)
    _pressure_paused: bool = field(default=False, repr=False)
    _flood_detected: bool = field(default=False, repr=False)
    _capture_fail_count: int = field(default=0, repr=False)
    _flood_ticks: int = field(default=0, repr=False)
    _status_updated_at: float = field(default=0.0, repr=False)
    # Intelligence fields
    _tool_invocations: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500), repr=False)
    _file_operations: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500), repr=False)
    _git_operations: collections.deque = field(default_factory=lambda: collections.deque(maxlen=200), repr=False)
    _test_results: collections.deque = field(default_factory=lambda: collections.deque(maxlen=50), repr=False)
    _snapshots: list = field(default_factory=list, repr=False)
    _status_history: list = field(default_factory=list, repr=False)
    _last_parse_index: int = field(default=0, repr=False)
    _total_lines_added: int = field(default=0, repr=False)
    _overflow_to_archive: tuple | None = field(default=None, repr=False)
    # Notes and tags
    notes: str = ""
    tags: list = field(default_factory=list)
    bookmarks: list = field(default_factory=list)

    def set_status(self, new_status: str) -> bool:
        """Update status with monotonic timestamp guard. Returns True if updated."""
        now = time.monotonic()
        if now >= self._status_updated_at:
            old_status = self.status
            self.status = new_status
            self._status_updated_at = now
            if new_status in ("error", "paused") and self.needs_input:
                self.needs_input = False
                self.input_prompt = None
            if old_status != new_status:
                self._status_history.append({"status": new_status, "at": now})
                if len(self._status_history) > 100:
                    self._status_history = self._status_history[-100:]
            return True
        return False

    def _get_status_timeline(self) -> list[dict]:
        """Build a timeline of status durations for visualization."""
        if not self._status_history:
            return []
        timeline = []
        now = time.monotonic()
        for i, entry in enumerate(self._status_history):
            end_time = self._status_history[i + 1]["at"] if i + 1 < len(self._status_history) else now
            duration_sec = round(end_time - entry["at"], 1)
            if duration_sec > 0:
                timeline.append({"status": entry["status"], "duration_sec": duration_sec})
        return timeline

    def _cost_burn_rate(self) -> dict | None:
        """Calculate cost burn rate ($/min) and estimated time to context exhaustion."""
        if self._spawn_time == 0.0 or self.estimated_cost_usd <= 0:
            return None
        uptime_min = (time.monotonic() - self._spawn_time) / 60.0
        if uptime_min < 0.5:
            return None
        rate_per_min = self.estimated_cost_usd / uptime_min
        total_tokens = self.tokens_input + self.tokens_output
        tokens_per_min = total_tokens / uptime_min if uptime_min > 0 else 0
        ctx_window = KNOWN_BACKENDS[self.backend].context_window if self.backend in KNOWN_BACKENDS else 200_000
        remaining_tokens = max(0, ctx_window - self.tokens_input)
        minutes_remaining = remaining_tokens / tokens_per_min if tokens_per_min > 0 else None
        return {
            "cost_per_min": round(rate_per_min, 4),
            "tokens_per_min": round(tokens_per_min),
            "minutes_remaining": round(minutes_remaining, 1) if minutes_remaining else None,
            "uptime_min": round(uptime_min, 1),
        }

    def create_snapshot(self, trigger: str) -> "OutputSnapshot":
        """Create a snapshot of current output state."""
        tail_lines = list(self.output_lines)[-50:]
        snap = OutputSnapshot(
            id=uuid.uuid4().hex[:8],
            agent_id=self.id,
            trigger=trigger,
            status=self.status,
            summary=self.summary,
            line_count=self.total_output_lines,
            output_tail="\n".join(tail_lines),
            context_pct=self.context_pct,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._snapshots.append(snap)
        if len(self._snapshots) > 20:
            self._snapshots = self._snapshots[-20:]
        return snap

    def to_dict(self) -> dict:
        role_obj = BUILTIN_ROLES.get(self.role)
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "role_icon": role_obj.icon if role_obj else "bot",
            "role_color": role_obj.color if role_obj else "#64748B",
            "status": self.status,
            "working_dir": self.working_dir,
            "backend": self.backend,
            "task": redact_secrets(self.task) if self.task else "",
            "summary": self.summary,
            "context_pct": self.context_pct,
            "memory_mb": self.memory_mb,
            "cpu_pct": self.cpu_pct,
            "needs_input": self.needs_input,
            "input_prompt": self.input_prompt,
            "error_message": self.error_message,
            "project_id": self.project_id,
            "pid": self.pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress_pct": self.progress_pct,
            "phase": self.phase,
            "related_agents": self.related_agents,
            "unread_messages": self.unread_messages,
            "restart_count": self.restart_count,
            "max_restarts": self.max_restarts,
            "restarted_at": self.restarted_at,
            "health_score": round(self.health_score, 3),
            "efficiency": calculate_efficiency_score(self),
            "error_count": self.error_count,
            "time_to_first_output": round(self.time_to_first_output, 2),
            "total_output_lines": self.total_output_lines,
            "total_output_chars": self._total_chars,
            "output_rate": round(self.output_rate, 1),
            "flood_detected": self._flood_detected,
            "files_touched": self.files_touched,
            "model": self.model,
            "tools_allowed": self.tools_allowed,
            "workflow_run_id": self.workflow_run_id,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "cost_burn_rate": self._cost_burn_rate(),
            "plan_mode": self.plan_mode,
            "git_branch": self.git_branch,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "cost_is_estimated": True,
            "context_window": KNOWN_BACKENDS[self.backend].context_window if self.backend in KNOWN_BACKENDS else 200_000,
            "tool_invocations_count": len(self._tool_invocations),
            "file_operations_count": len(self._file_operations),
            "last_test_result": self._test_results[-1].to_dict() if self._test_results else None,
            "snapshot_count": len(self._snapshots),
            "status_timeline": self._get_status_timeline(),
            "notes": self.notes,
            "tags": list(self.tags),
            "bookmarks": list(self.bookmarks),
        }

    def to_dict_full(self) -> dict:
        d = self.to_dict()
        d["output_lines"] = list(self.output_lines)
        return d


# ─────────────────────────────────────────────
# System Metrics
# ─────────────────────────────────────────────


@dataclass
class SystemMetrics:
    cpu_pct: float = 0.0
    cpu_count: int = 0
    memory_total_gb: float = 0.0
    memory_used_gb: float = 0.0
    memory_available_gb: float = 0.0
    memory_pct: float = 0.0
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_pct: float = 0.0
    load_avg: list = field(default_factory=list)
    agents_active: int = 0
    agents_total: int = 0

    def to_dict(self) -> dict:
        return {
            "cpu_pct": self.cpu_pct,
            "cpu_count": self.cpu_count,
            "memory": {
                "total_gb": self.memory_total_gb,
                "used_gb": self.memory_used_gb,
                "available_gb": self.memory_available_gb,
                "pct": self.memory_pct,
            },
            "disk": {
                "total_gb": self.disk_total_gb,
                "used_gb": self.disk_used_gb,
                "pct": self.disk_pct,
            },
            "load_avg": self.load_avg,
            "agents_active": self.agents_active,
            "agents_total": self.agents_total,
        }


# ─────────────────────────────────────────────
# Intelligence Data Models
# ─────────────────────────────────────────────


@dataclass
class ToolInvocation:
    """A parsed tool call from agent output."""
    agent_id: str
    tool: str
    args: str
    timestamp: float
    line_index: int
    result_status: str = "unknown"
    result_snippet: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "tool": self.tool,
            "args": self.args,
            "timestamp": self.timestamp,
            "result_status": self.result_status,
            "result_snippet": self.result_snippet,
        }


@dataclass
class FileOperation:
    """A file read/write/create detected from agent output."""
    agent_id: str
    file_path: str
    operation: str
    timestamp: float
    tool: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "file_path": self.file_path,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "tool": self.tool,
        }


@dataclass
class GitOperation:
    """A git operation detected from agent output."""
    agent_id: str
    operation: str
    detail: str
    timestamp: float
    files_affected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "operation": self.operation,
            "detail": self.detail,
            "timestamp": self.timestamp,
            "files_affected": self.files_affected,
        }


@dataclass
class AgentTestResult:
    """Parsed test run results from agent output."""
    agent_id: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    coverage_pct: float | None = None
    framework: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
            "coverage_pct": self.coverage_pct,
            "framework": self.framework,
            "timestamp": self.timestamp,
        }


@dataclass
class AgentInsight:
    """A cross-agent insight or alert from the meta-agent."""
    id: str
    insight_type: str
    severity: str
    message: str
    agent_ids: list[str] = field(default_factory=list)
    evidence: str = ""
    suggested_action: str = ""
    acknowledged: bool = False
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "insight_type": self.insight_type,
            "severity": self.severity,
            "message": self.message,
            "agent_ids": self.agent_ids,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
            "acknowledged": self.acknowledged,
            "created_at": self.created_at,
        }


# ─────────────────────────────────────────────
# Orchestration Models
# ─────────────────────────────────────────────


@dataclass
class QueuedTask:
    """A task waiting to be auto-spawned when agent slots are available."""
    id: str
    role: str
    name: str
    task: str
    working_dir: str = ""
    backend: str = ""
    plan_mode: bool = False
    project_id: str | None = None
    priority: int = 0
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "task": self.task,
            "working_dir": self.working_dir,
            "backend": self.backend,
            "plan_mode": self.plan_mode,
            "project_id": self.project_id,
            "priority": self.priority,
            "created_at": self.created_at,
        }


@dataclass
class ParsedIntent:
    """Result of NLU command parsing."""
    action: str
    targets: list[str] = field(default_factory=list)
    filter: str = ""
    message: str = ""
    parameters: dict = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "targets": self.targets,
            "filter": self.filter,
            "message": self.message,
            "parameters": self.parameters,
            "confidence": self.confidence,
        }


@dataclass
class WorkflowRun:
    """Tracks a running workflow pipeline with dependency management."""
    id: str
    workflow_id: str
    workflow_name: str
    agent_specs: list[dict]
    agent_map: dict[int, str] = field(default_factory=dict)
    pending_indices: set[int] = field(default_factory=set)
    running_ids: set[str] = field(default_factory=set)
    completed_ids: set[str] = field(default_factory=set)
    failed_ids: set[str] = field(default_factory=set)
    status: str = "running"
    working_dir: str = ""
    created_at: str = ""
    completed_at: str | None = None
    stage_timeout_sec: float = 1800.0
    stage_started_at: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "agent_specs": self.agent_specs,
            "agent_map": {str(k): v for k, v in self.agent_map.items()},
            "pending_indices": list(self.pending_indices),
            "running_ids": list(self.running_ids),
            "completed_ids": list(self.completed_ids),
            "failed_ids": list(self.failed_ids),
            "status": self.status,
            "working_dir": self.working_dir,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "stage_timeout_sec": self.stage_timeout_sec,
        }

    @staticmethod
    def detect_circular_deps(agent_specs: list[dict]) -> list[list[int]] | None:
        """Detect circular dependencies in workflow spec. Returns list of cycles, or None if DAG is valid."""
        n = len(agent_specs)
        adj: dict[int, list[int]] = {i: spec.get("depends_on", []) for i, spec in enumerate(agent_specs)}

        for idx, deps in adj.items():
            for d in deps:
                if not isinstance(d, int) or d < 0 or d >= n:
                    return [[idx, d]]

        WHITE, GRAY, BLACK = 0, 1, 2
        color = [WHITE] * n
        cycles: list[list[int]] = []

        def dfs(u: int, path: list[int]) -> None:
            color[u] = GRAY
            path.append(u)
            for v in adj.get(u, []):
                if color[v] == GRAY:
                    cycle_start = path.index(v)
                    cycles.append(path[cycle_start:] + [v])
                elif color[v] == WHITE:
                    dfs(v, path)
            path.pop()
            color[u] = BLACK

        for i in range(n):
            if color[i] == WHITE:
                dfs(i, [])

        return cycles if cycles else None


# ─────────────────────────────────────────────
# Utility Functions
# ─────────────────────────────────────────────


def calculate_efficiency_score(agent: Agent) -> dict:
    """Calculate efficiency metrics: tools/min, error rate, context efficiency, productivity."""
    now = time.monotonic()
    uptime_min = max(0.1, (now - agent._spawn_time) / 60) if agent._spawn_time != 0.0 else 0.1

    tool_count = len(agent._tool_invocations) if hasattr(agent, '_tool_invocations') else 0
    tools_per_min = round(tool_count / uptime_min, 2)
    tool_score = min(1.0, tools_per_min / 6.0) if tools_per_min > 0 else 0.0

    file_count = len(agent._file_operations) if hasattr(agent, '_file_operations') else 0
    files_per_min = round(file_count / uptime_min, 2)

    error_rate = agent.error_count / max(1, tool_count + file_count) if (tool_count + file_count) > 0 else 0.0
    error_score = max(0.0, 1.0 - error_rate * 5.0)

    ctx_used = max(0.01, agent.context_pct)
    ctx_score = min(1.0, (tool_count + file_count) / (ctx_used * 100)) if ctx_used > 0.01 else 0.5

    total_lines = agent.total_output_lines or len(list(agent.output_lines))
    lines_per_min = round(total_lines / uptime_min, 1)

    composite = round(
        tool_score * 0.35 +
        error_score * 0.30 +
        ctx_score * 0.20 +
        min(1.0, lines_per_min / 50) * 0.15,
        3
    )

    return {
        "score": min(1.0, max(0.0, composite)),
        "tools_per_min": tools_per_min,
        "files_per_min": files_per_min,
        "error_rate": round(error_rate, 3),
        "lines_per_min": lines_per_min,
        "context_efficiency": round(ctx_score, 3),
        "uptime_min": round(uptime_min, 1),
    }
