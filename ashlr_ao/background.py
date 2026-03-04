"""
Ashlr AO — Background Tasks

Six supervised background task loops: output capture, metrics,
health checking, memory watchdog, meta-agent intelligence, and
archive cleanup. Plus task supervisor and startup/shutdown helpers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiohttp import web

from ashlr_ao.constants import _strip_ansi
from ashlr_ao.intelligence import (
    IntelligenceClient,
    _alert_throttle,
    _intelligence_parser,
    calculate_health_score,
    estimate_progress,
)
from ashlr_ao.licensing import (
    COMMUNITY_LICENSE,
    _effective_max_agents,
    validate_license,
)
from ashlr_ao.middleware import RateLimiter
from ashlr_ao.models import AgentInsight
from ashlr_ao.status import _suggest_followup, extract_summary
from ashlr_ao.websocket import WebSocketHub, collect_system_metrics

if TYPE_CHECKING:
    from ashlr_ao.config import Config
    from ashlr_ao.database import Database
    from ashlr_ao.manager import AgentManager

log = logging.getLogger("ashlr")

# ─────────────────────────────────────────────
# Auto-approve safety: never-approve patterns
# ─────────────────────────────────────────────
_NEVER_APPROVE_PATTERNS = [
    re.compile(r"(?i)\brm\s+(-[a-z]*r[a-z]*\b|-r\b|-rf\b|-fr\b)"),  # rm -rf, rm -r, rm -fr, rm -rfi, etc.
    re.compile(r"(?i)\bforce\s+push\b"),
    re.compile(r"(?i)\bgit\s+push\s+.*--force"),  # includes --force-with-lease
    re.compile(r"(?i)\bgit\s+push\s+\S+\s+:"),  # git push origin :branch (remote branch delete)
    re.compile(r"(?i)\bgit\s+reset\s+--hard\b"),
    re.compile(r"(?i)\bdrop\s+(?:table|database)\b"),
    re.compile(r"(?i)\btruncate\s+table\b"),
    re.compile(r"(?i)\bformat\s+(?:c:|disk)\b"),
    re.compile(r"(?i)\bsudo\s+rm\b"),
    re.compile(r"(?i)\bchmod\s+777\b"),
    re.compile(r"(?i)\b(?:delete|destroy)\s+(?:all|everything|production|prod)\b"),
    re.compile(r"(?i)\bdd\s+.*\bof=/dev/"),  # dd writing to device
    re.compile(r"(?i)\bmkfs\b"),  # filesystem format
    re.compile(r"(?i)\bcurl\s+.*\|\s*(?:ba)?sh\b"),  # curl | bash
    re.compile(r"(?i)\bwget\s+.*\|\s*(?:ba)?sh\b"),  # wget | bash
    re.compile(r"(?i)\bfind\s+.*-delete\b"),  # find -delete
]

# Per-agent auto-approve rate limiter: {agent_id: [monotonic_timestamps]}
_auto_approve_history: dict[str, list[float]] = {}
_AUTO_APPROVE_MAX_PER_MINUTE = 5


def _check_auto_approve(prompt: str, patterns: list[dict], agent_id: str) -> str | None:
    """Check if an agent's input prompt matches an auto-approve pattern.

    Returns the response string if matched (and safe), or None if no match / blocked by safety.
    """
    # Rate limit check
    now = time.monotonic()
    history = _auto_approve_history.get(agent_id, [])
    history = [t for t in history if now - t < 60.0]
    _auto_approve_history[agent_id] = history
    if len(history) >= _AUTO_APPROVE_MAX_PER_MINUTE:
        log.warning(f"Auto-approve rate limit hit for agent {agent_id} ({len(history)} in last 60s)")
        return None

    # Safety check: never approve destructive patterns
    for never_pat in _NEVER_APPROVE_PATTERNS:
        if never_pat.search(prompt):
            log.warning(f"Auto-approve blocked by safety pattern for agent {agent_id}: {never_pat.pattern}")
            return None

    # Match against configured patterns
    for pat_config in patterns:
        pattern_str = pat_config.get("pattern", "")
        response = pat_config.get("response", "yes")
        if not pattern_str:
            continue
        try:
            if re.search(pattern_str, prompt, re.IGNORECASE):
                # Record approval
                _auto_approve_history.setdefault(agent_id, []).append(now)
                return response
        except re.error:
            log.warning(f"Invalid auto-approve regex: {pattern_str!r}")
    return None


# ─────────────────────────────────────────────
# Section 9: Background Tasks
# ─────────────────────────────────────────────

async def output_capture_loop(app: web.Application) -> None:
    """Capture output from all agents every ~1 second."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    interval = app["config"].output_capture_interval
    _archive_retry_queue: list[tuple[str, list[str], int]] = []  # (agent_id, lines, offset)
    _seen_conflicts: set[tuple[str, frozenset]] = set()  # Dedup file conflict notifications
    _recent_errors: list[tuple[float, str]] = []  # (timestamp, agent_id) for multi-error detection
    _fleet_error_warned = False

    while True:
        try:
            # Retry previously failed archival (max 5 per iteration)
            if _archive_retry_queue and app.get("db_available", True):
                db_retry: Database = app["db"]
                retried = 0
                still_pending: list[tuple[str, list[str], int]] = []
                for item in _archive_retry_queue:
                    if retried >= 5:
                        still_pending.append(item)
                        continue
                    try:
                        await db_retry.archive_output(item[0], item[1], item[2])
                        retried += 1
                    except Exception:
                        still_pending.append(item)
                        retried += 1
                _archive_retry_queue = still_pending

            # Phase 1: Parallel output capture
            active_agents = [
                (aid, agent) for aid, agent in list(manager.agents.items())
                if agent.status not in ("paused", "killed")
            ]

            # Check spawn timeouts first (cheap, no I/O)
            for agent_id, agent in active_agents:
                if agent.status == "spawning" and agent._spawn_time != 0.0:
                    if time.monotonic() - agent._spawn_time > 30:
                        agent.set_status("error")
                        agent.error_message = "Spawn timeout — no output after 30s"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_error", f"Agent {agent.name} spawn timed out", agent_id, agent.name)

            # Parallel capture — all tmux reads happen concurrently
            async def _capture_one(aid: str) -> tuple[str, list[str] | None]:
                try:
                    lines = await manager.capture_output(aid)
                    return (aid, lines)
                except Exception as e:
                    log.warning(f"Output capture error for {aid}: {e}")
                    return (aid, None)

            capture_tasks = [_capture_one(aid) for aid, agent in active_agents if agent.status != "error"]
            results = await asyncio.gather(*capture_tasks, return_exceptions=True)

            # Phase 2: Sequential processing of capture results
            for result in results:
                if isinstance(result, Exception):
                    log.warning(f"Capture task exception: {result}")
                    continue
                agent_id, new_lines = result
                agent = manager.agents.get(agent_id)
                if not agent:
                    continue

                if new_lines is None:
                    # Track consecutive capture failures — only error after 3
                    agent._capture_fail_count = getattr(agent, '_capture_fail_count', 0) + 1
                    if agent._capture_fail_count >= 3:
                        agent.set_status("error")
                        agent.error_message = "Output capture failed (3 consecutive failures)"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    continue

                # Reset consecutive failure counter on successful capture
                agent._capture_fail_count = 0

                if new_lines:
                    now_mono = time.monotonic()

                    # -- Per-agent metrics tracking --
                    if not agent._first_output_received and agent._spawn_time != 0.0:
                        agent._first_output_received = True
                        agent.time_to_first_output = round(now_mono - agent._spawn_time, 2)

                    agent.last_output_time = now_mono
                    agent._stale_warned = False
                    line_count = len(new_lines)
                    agent.total_output_lines += line_count
                    agent._output_line_timestamps.append((now_mono, line_count))

                    # Archive overflow lines to DB if flagged by capture_output
                    if agent._overflow_to_archive:
                        aid_arch, overflow_lines, offset = agent._overflow_to_archive
                        agent._overflow_to_archive = None
                        if app.get("db_available", True):
                            db: Database = app["db"]
                            try:
                                await db.archive_output(aid_arch, overflow_lines, offset)
                            except Exception as e:
                                log.debug(f"Output archiving failed for {agent_id}: {e}")
                                # Queue for retry (cap at 50 to avoid unbounded growth)
                                if len(_archive_retry_queue) < 50:
                                    _archive_retry_queue.append((aid_arch, overflow_lines, offset))

                    # Rolling output rate
                    cutoff = now_mono - 60.0
                    recent_lines_count = sum(
                        count for ts, count in agent._output_line_timestamps if ts >= cutoff
                    )
                    window = min(60.0, now_mono - agent._spawn_time) if agent._spawn_time != 0.0 else 60.0
                    agent.output_rate = (recent_lines_count / max(window, 1.0)) * 60.0

                    # Flood detection: flag agents producing excessive output
                    flood_threshold = app["config"].flood_threshold_lines_per_min
                    if agent.output_rate > flood_threshold:
                        agent._flood_ticks += 1
                        if agent._flood_ticks >= app["config"].flood_sustained_ticks and not agent._flood_detected:
                            agent._flood_detected = True
                            log.warning(
                                f"Agent {agent_id} ({agent.name}) flood detected: "
                                f"{agent.output_rate:.0f} lines/min (threshold: {flood_threshold})"
                            )
                            await hub.broadcast_event(
                                "agent_flood",
                                f"Agent {agent.name} is producing excessive output ({agent.output_rate:.0f} lines/min). "
                                "Output broadcast throttled.",
                                agent_id, agent.name,
                            )
                    else:
                        agent._flood_ticks = max(0, agent._flood_ticks - 1)
                        if agent._flood_detected and agent._flood_ticks == 0:
                            agent._flood_detected = False
                            log.info(f"Agent {agent_id} ({agent.name}) flood cleared")

                    # Broadcast output (skip broadcast for flooded agents to protect WS clients)
                    if not agent._flood_detected:
                        await hub.broadcast({
                            "type": "agent_output",
                            "agent_id": agent_id,
                            "lines": new_lines,
                        })
                    elif len(new_lines) > 0:
                        # Still broadcast a summary for flooded agents (every 10th capture)
                        if agent._flood_ticks % 10 == 0:
                            await hub.broadcast({
                                "type": "agent_output",
                                "agent_id": agent_id,
                                "lines": [f"[{len(new_lines)} lines suppressed — output flood detected]"],
                            })

                    # File conflict detection (deduplicate — only report each conflict pair+file once)
                    try:
                        conflicts = manager._check_file_conflicts(agent_id, new_lines)
                        for conflict in conflicts:
                            conflict_key = (conflict['file_path'], frozenset([conflict['agent_id'], conflict['other_agent_id']]))
                            if conflict_key not in _seen_conflicts:
                                _seen_conflicts.add(conflict_key)
                                await hub.broadcast_event(
                                    "file_conflict",
                                    f"File conflict: {conflict['agent_name']} and {conflict['other_agent_name']} both working on {conflict['file_path']}",
                                    agent_id, agent.name,
                                    conflict,
                                )
                    except Exception as e:
                        log.debug(f"File conflict detection error for {agent_id}: {e}")

                    # Update summary
                    agent.summary = extract_summary(list(agent.output_lines), agent.task, agent.status)
                    agent.progress_pct = estimate_progress(agent)

                    # Structured intelligence parsing (pure regex, ~0.1ms)
                    try:
                        parse_counts = _intelligence_parser.parse_incremental(agent)
                        if any(parse_counts.values()):
                            activity = _intelligence_parser.get_activity_summary(agent)
                            await hub.broadcast({
                                "type": "agent_activity",
                                "agent_id": agent_id,
                                "activity": activity,
                                "new_counts": parse_counts,
                            })
                    except Exception as e:
                        log.debug(f"Intelligence parser error for {agent_id}: {e}")

                    # Pattern alerting — check new lines against configured alert patterns
                    try:
                        compiled_alerts = app.get("_compiled_alert_patterns")
                        if compiled_alerts:
                            for line in new_lines:
                                stripped_line = _strip_ansi(line) if callable(_strip_ansi) else line
                                for pat_re, severity, label in compiled_alerts:
                                    if pat_re.search(stripped_line):
                                        # Throttle: max 1 alert per pattern per agent per 30s
                                        alert_key = f"{agent_id}:{label}"
                                        last_alert_time = _alert_throttle.get(alert_key, 0)
                                        if now_mono - last_alert_time >= 30.0:
                                            _alert_throttle[alert_key] = now_mono
                                            await hub.broadcast({
                                                "type": "pattern_alert",
                                                "agent_id": agent_id,
                                                "agent_name": agent.name,
                                                "severity": severity,
                                                "label": label,
                                                "line": stripped_line[:300],
                                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                            })
                                            log.warning(f"Pattern alert [{severity}] {label} on agent {agent.name}: {stripped_line[:100]}")
                                        break  # One alert per line max
                    except Exception as e:
                        log.debug(f"Pattern alerting error for {agent_id}: {e}")

                    # LLM summary with throttling + output-change cache
                    summarizer: IntelligenceClient | None = app.get("intelligence")
                    if summarizer and summarizer.available:
                        if now_mono - agent._last_llm_summary_time >= app["config"].llm_summary_interval:
                            # Skip LLM call if output hasn't changed since last summary
                            current_hash = agent._prev_output_hash
                            if current_hash != agent._summary_output_hash:
                                agent._last_llm_summary_time = now_mono
                                try:
                                    llm_summary = await summarizer.summarize(
                                        list(agent.output_lines), agent.task,
                                        agent.role, agent.status
                                    )
                                    if llm_summary:
                                        agent.summary = llm_summary
                                        agent._llm_summary = llm_summary
                                        agent._summary_output_hash = current_hash
                                except Exception as e:
                                    log.debug(f"LLM summary failed for {agent_id}: {e}")
                            else:
                                # Output unchanged — skip LLM call, reset timer
                                agent._last_llm_summary_time = now_mono

                # Output staleness detection (runs whether or not there were new lines)
                if agent.status in ("working", "planning") and agent.last_output_time != 0.0:
                    silence = time.monotonic() - agent.last_output_time
                    stall_threshold = app["config"].stall_timeout_minutes * 60
                    if silence > max(stall_threshold, 900):
                        # Auto-restart on stall (Wave 3): try restart before marking error
                        if (
                            app["config"].auto_restart_on_stall
                            and agent.restart_count < agent.max_restarts
                        ):
                            log.info(
                                f"Auto-restarting stalled agent {agent_id} ({agent.name}) — "
                                f"no output for {int(silence)}s, attempt {agent.restart_count + 1}/{agent.max_restarts}"
                            )
                            try:
                                success = await manager.restart(agent_id)
                                if success:
                                    restarted = manager.agents.get(agent_id)
                                    if restarted:
                                        restarted._stale_warned = False
                                        await hub.broadcast({"type": "agent_update", "agent": restarted.to_dict()})
                                        await hub.broadcast_event(
                                            "agent_stall_restarted",
                                            f"Agent {agent.name} auto-restarted after {int(silence)}s stall (attempt {restarted.restart_count})",
                                            agent_id, agent.name,
                                        )
                                    continue
                                else:
                                    log.warning(f"Auto-restart on stall failed for {agent_id}")
                            except Exception as e:
                                log.error(f"Auto-restart on stall error for {agent_id}: {e}")
                        # Fall through to error if restart not enabled/failed/exhausted
                        agent.set_status("error")
                        agent.error_message = f"No output for {int(silence // 60)} minutes"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_stale", f"Agent {agent.name} stale — no output for {int(silence // 60)} minutes", agent_id, agent.name)
                        continue  # Skip detect_status — don't let it revert error
                    elif silence > min(stall_threshold, 300):
                        if not agent._stale_warned:
                            agent._stale_warned = True
                            await hub.broadcast_event("agent_stale_warning", f"Agent {agent.name} has had no output for {int(silence)}s", agent_id, agent.name)

                # Health score
                try:
                    agent.health_score = calculate_health_score(agent, app["config"].memory_limit_mb)
                except Exception as e:
                    log.warning(f"Health score calculation failed for {agent_id}: {e}")

                # Health-driven events (using configurable thresholds)
                _crit_thresh = app["config"].health_critical_threshold
                _low_thresh = app["config"].health_low_threshold
                if agent.health_score < _crit_thresh:
                    if not getattr(agent, '_health_critical_warned', False):
                        agent._health_critical_warned = True
                        await hub.broadcast_event(
                            "agent_health_critical",
                            f"Agent {agent.name} health critical ({int(agent.health_score * 100)}%) — consider restarting",
                            agent_id, agent.name,
                        )
                        # Auto-pause on critical health (Wave 3)
                        if (
                            app["config"].auto_pause_on_critical_health
                            and agent.status in ("working", "planning", "reading")
                        ):
                            log.info(f"Auto-pausing agent {agent_id} ({agent.name}) — health critical at {agent.health_score:.0%}")
                            try:
                                await manager.pause(agent_id)
                                await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                                await hub.broadcast_event(
                                    "agent_health_auto_paused",
                                    f"Agent {agent.name} auto-paused — health critical ({int(agent.health_score * 100)}%)",
                                    agent_id, agent.name,
                                )
                            except Exception as hp_err:
                                log.warning(f"Health auto-pause failed for {agent_id}: {hp_err}")
                elif agent.health_score < _low_thresh:
                    if not getattr(agent, '_health_low_warned', False):
                        agent._health_low_warned = True
                        await hub.broadcast_event(
                            "agent_health_low",
                            f"Agent {agent.name} health low ({int(agent.health_score * 100)}%)",
                            agent_id, agent.name,
                        )
                elif agent.health_score >= 0.5:
                    # Reset warning flags when health recovers
                    agent._health_low_warned = False
                    agent._health_critical_warned = False

                # Detect status
                try:
                    new_status = await manager.detect_status(agent_id)
                    if new_status != agent.status:
                        old_status = agent.status
                        # Plan approved: clear plan_mode when agent goes waiting → working
                        if agent.plan_mode and old_status == "waiting" and new_status == "working":
                            agent.plan_mode = False
                        agent.set_status(new_status)
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        log.debug(f"Agent {agent_id} status: {old_status} -> {new_status}")

                        # Auto-snapshot on key status transitions
                        if new_status in ("error", "waiting", "idle"):
                            try:
                                agent.create_snapshot(trigger=new_status if new_status != "idle" else "complete")
                            except Exception as snap_err:
                                log.debug(f"Snapshot creation failed for {agent_id}: {snap_err}")

                        if new_status == "error" and old_status != "error":
                            agent._error_entered_at = time.monotonic()
                            # Reset health warning flags so they can re-fire on recovery+relapse
                            agent._health_low_warned = False
                            agent._health_critical_warned = False
                            # Track for multi-error detection
                            now_mono = time.monotonic()
                            _recent_errors.append((now_mono, agent_id))
                            _recent_errors[:] = [(t, a) for t, a in _recent_errors if now_mono - t < 60]
                            if len(_recent_errors) >= 2 and not _fleet_error_warned:
                                err_names = [manager.agents[a].name for _, a in _recent_errors if a in manager.agents]
                                _fleet_error_warned = True
                                await hub.broadcast_event(
                                    "fleet_multi_error",
                                    f"Multiple agents errored: {', '.join(err_names)}",
                                    agent_id, agent.name,
                                )
                            elif len(_recent_errors) < 2:
                                _fleet_error_warned = False
                            wf_run, failure_action = manager.on_agent_failed(agent_id)
                            if wf_run:
                                if failure_action == "retry":
                                    await hub.broadcast_event(
                                        "workflow_agent_retry",
                                        f"Pipeline: retrying agent {agent.name}",
                                        agent_id, agent.name,
                                        {"workflow_run_id": wf_run.id},
                                    )
                                elif failure_action == "skip":
                                    await hub.broadcast_event(
                                        "workflow_agent_skipped",
                                        f"Pipeline: skipping failed agent {agent.name} (on_failure=skip)",
                                        agent_id, agent.name,
                                        {"workflow_run_id": wf_run.id},
                                    )
                                await manager.resolve_workflow_deps(wf_run, hub)

                        if new_status == "idle" and old_status != "idle":
                            wf_run = manager.on_agent_complete(agent_id)
                            if wf_run:
                                await manager.resolve_workflow_deps(wf_run, hub)

                            # Broadcast completion event with summary
                            duration = time.monotonic() - agent._spawn_time if agent._spawn_time else 0
                            dur_str = f"{int(duration // 60)}m{int(duration % 60)}s" if duration > 60 else f"{int(duration)}s"
                            cost_str = f"${agent.estimated_cost_usd:.2f}" if agent.estimated_cost_usd > 0 else ""
                            completion_msg = f"Agent {agent.name} completed"
                            if agent.summary:
                                completion_msg += f": {agent.summary}"
                            await hub.broadcast_event(
                                "agent_completed",
                                completion_msg,
                                agent_id, agent.name,
                                metadata={"duration": dur_str, "cost": cost_str, "summary": agent.summary or ""},
                            )

                            # Suggest follow-up agents based on what this agent did
                            suggestion = _suggest_followup(agent)
                            if suggestion:
                                await hub.broadcast_event(
                                    "agent_followup_suggestion",
                                    suggestion["message"],
                                    agent_id, agent.name,
                                    metadata=suggestion,
                                )

                            # Auto-handoff: spawn successor agent if configured (Wave 5)
                            if getattr(agent, "next_agent_config", None):
                                next_cfg = agent.next_agent_config
                                agent.next_agent_config = None  # Prevent re-triggering
                                try:
                                    # Substitute handoff variables in task
                                    task_text = next_cfg.get("task", "")
                                    task_text = task_text.replace("{prev_summary}", agent.summary or "")
                                    task_text = task_text.replace("{prev_agent}", agent.name)
                                    now_dt = datetime.now(timezone.utc)
                                    task_text = task_text.replace("{date}", now_dt.strftime("%Y-%m-%d"))
                                    task_text = task_text.replace("{time}", now_dt.strftime("%H:%M"))

                                    next_id = await manager.spawn(
                                        role=next_cfg.get("role", "general"),
                                        task=task_text,
                                        name=next_cfg.get("name", ""),
                                        working_dir=next_cfg.get("working_dir", agent.working_dir),
                                        backend=next_cfg.get("backend", agent.backend),
                                        model=next_cfg.get("model", ""),
                                        project_id=agent.project_id,
                                    )
                                    next_agent = manager.agents.get(next_id)
                                    if next_agent:
                                        # Don't inherit next_agent_config (no recursive handoffs)
                                        next_agent.next_agent_config = None
                                        await hub.broadcast({"type": "agent_update", "agent": next_agent.to_dict()})
                                        await hub.broadcast_event(
                                            "agent_handoff",
                                            f"Agent {agent.name} handed off to {next_agent.name}",
                                            next_id, next_agent.name,
                                            metadata={"from_agent": agent_id, "from_name": agent.name},
                                        )
                                except Exception as ho_err:
                                    log.warning(f"Auto-handoff failed for {agent_id}: {ho_err}")
                                    await hub.broadcast_event(
                                        "agent_handoff_failed",
                                        f"Handoff from {agent.name} failed: {ho_err}",
                                        agent_id, agent.name,
                                    )

                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

                        if agent.needs_input:
                            now_mono = time.monotonic()
                            if now_mono - agent._last_needs_input_event > 5.0:
                                agent._last_needs_input_event = now_mono

                                # Auto-approve pattern matching (Wave 3)
                                auto_approved = False
                                if app["config"].auto_approve_enabled:
                                    prompt_text = agent.input_prompt or ""
                                    # Merge global + per-project patterns
                                    all_patterns = list(app["config"].auto_approve_patterns)
                                    if agent.project_id:
                                        try:
                                            db: Database = app["db"]
                                            project = await db.get_project(agent.project_id)
                                            if project and project.get("auto_approve_patterns"):
                                                all_patterns.extend(project.get("auto_approve_patterns", []))
                                        except Exception:
                                            pass  # DB unavailable, use global only

                                    if all_patterns:
                                        response = _check_auto_approve(prompt_text, all_patterns, agent_id)
                                        if response is not None:
                                            auto_approved = True
                                            log.info(f"Auto-approving agent {agent_id} ({agent.name}): sending {response!r}")
                                            try:
                                                await manager.send_message(agent_id, response)
                                                await hub.broadcast_event(
                                                    "agent_auto_approved",
                                                    f"Agent {agent.name} auto-approved: sent {response!r} in response to: {prompt_text[:100]}",
                                                    agent_id, agent.name,
                                                    metadata={"response": response, "prompt": prompt_text[:200]},
                                                )
                                            except Exception as aa_err:
                                                log.warning(f"Auto-approve send failed for {agent_id}: {aa_err}")
                                                auto_approved = False

                                if not auto_approved:
                                    await hub.broadcast_event(
                                        "agent_needs_input",
                                        agent.input_prompt or "Agent needs input",
                                        agent_id, agent.name,
                                    )
                    else:
                        if new_lines:
                            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                except Exception as e:
                    log.debug(f"Status detection error for {agent_id}: {e}")

            # Prune _seen_conflicts for agents that no longer exist
            active_ids = set(manager.agents.keys())
            _seen_conflicts = {k for k in _seen_conflicts if k[1] & active_ids}

        except Exception as e:
            log.error(f"Output capture loop error: {e}")

        await asyncio.sleep(interval)


async def metrics_loop(app: web.Application) -> None:
    """Collect and broadcast system metrics every 2 seconds."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]

    while True:
        try:
            metrics = await collect_system_metrics(manager)

            # Also update per-agent memory and CPU
            for agent_id, agent in list(manager.agents.items()):
                try:
                    agent.memory_mb = await manager.get_agent_memory(agent_id)
                    agent.cpu_pct = await manager.get_agent_cpu(agent_id)
                except Exception as e:
                    log.warning(f"Failed to collect metrics for agent {agent_id}: {e}")

            await hub.broadcast({"type": "metrics", **metrics.to_dict()})
        except Exception as e:
            log.error(f"Metrics loop error: {e}")

        await asyncio.sleep(2.0)


async def health_check_loop(app: web.Application) -> None:
    """Verify tmux sessions are alive, clean up dead agents, auto-restart crashed agents."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    _tick = 0

    while True:
        _tick += 1
        # Periodic rate limiter + alert throttle cleanup (~every 300s at 5s interval)
        if _tick % 60 == 0:
            rl: RateLimiter | None = app.get("rate_limiter")
            if rl:
                rl.cleanup_stale()
            # Evict stale alert throttle entries (>120s old) and cap at 500
            now_mono = time.monotonic()
            stale_keys = [k for k, t in _alert_throttle.items() if now_mono - t > 120.0]
            for k in stale_keys:
                del _alert_throttle[k]
            if len(_alert_throttle) > 500:
                sorted_keys = sorted(_alert_throttle, key=_alert_throttle.get)  # type: ignore[arg-type]
                for k in sorted_keys[:len(_alert_throttle) - 500]:
                    del _alert_throttle[k]
            # Evict auto-approve history for dead agents
            active_ids = set(manager.agents.keys())
            stale_approve = [k for k in _auto_approve_history if k not in active_ids]
            for k in stale_approve:
                del _auto_approve_history[k]
        try:
            for agent_id, agent in list(manager.agents.items()):
                # -- Auto-restart logic for agents in error state --
                if agent.status == "error":
                    now = time.monotonic()
                    error_duration = now - agent._error_entered_at if agent._error_entered_at > 0 else 0

                    # Pathological error loop detection: if agent errored within window of last restart
                    if agent.restart_count > 0 and agent._error_entered_at > 0 and agent.last_restart_time > 0:
                        time_working = agent._error_entered_at - agent.last_restart_time
                        if time_working < app["config"].pathological_error_window_sec and not agent._pathological:
                            agent._pathological = True
                            agent.max_restarts = min(agent.max_restarts, app["config"].max_pathological_restarts)
                            log.warning(
                                f"Agent {agent_id} ({agent.name}) flagged as pathological — "
                                f"errored {time_working:.0f}s after restart (threshold: {app['config'].pathological_error_window_sec}s)"
                            )
                            await hub.broadcast_event(
                                "agent_pathological",
                                f"Agent {agent.name} is in a pathological error loop (errors within {time_working:.0f}s of restart). Auto-restart limited.",
                                agent_id, agent.name,
                            )

                    # Only attempt restart if error has persisted >10s
                    if error_duration > 10.0 and agent.restart_count < agent.max_restarts:
                        # Exponential backoff: 5s * 2^restart_count (5s, 10s, 20s)
                        backoff = 5.0 * (2 ** agent.restart_count)
                        time_since_last_restart = now - agent.last_restart_time if agent.last_restart_time > 0 else float("inf")

                        if time_since_last_restart >= backoff:
                            log.info(
                                f"Auto-restarting agent {agent_id} ({agent.name}), "
                                f"attempt {agent.restart_count + 1}/{agent.max_restarts}, "
                                f"backoff was {backoff:.0f}s"
                            )
                            try:
                                success = await manager.restart(agent_id)
                                if success:
                                    restarted_agent = manager.agents.get(agent_id)
                                    if restarted_agent:
                                        await hub.broadcast({"type": "agent_update", "agent": restarted_agent.to_dict()})
                                        await hub.broadcast_event("agent_restarted", f"Agent {agent.name} auto-restarted (attempt {restarted_agent.restart_count})", agent_id, agent.name)
                                else:
                                    log.warning(f"Auto-restart failed for agent {agent_id}")
                            except Exception as e:
                                log.error(f"Auto-restart error for {agent_id}: {e}")

                    elif agent.restart_count >= agent.max_restarts and agent._error_entered_at > 0:
                        # Max restarts exhausted — send notification once
                        # Use _error_entered_at as a flag: set to 0 after notification
                        agent._error_entered_at = 0
                        log.warning(
                            f"Agent {agent_id} ({agent.name}) exceeded max restarts "
                            f"({agent.max_restarts}), leaving in error state"
                        )
                        await hub.broadcast_event(
                            "agent_restart_exhausted",
                            f"Agent {agent.name} failed after {agent.max_restarts} restart attempts. Manual intervention required.",
                            agent_id, agent.name,
                        )
                    continue

                # -- Check if tmux session is still alive --
                exists = await manager._tmux_session_exists(agent.tmux_session)
                if not exists:
                    log.warning(f"Agent {agent_id} ({agent.name}) tmux session died")
                    agent.set_status("error")
                    agent.error_message = "tmux session terminated unexpectedly"
                    agent._error_entered_at = time.monotonic()
                    agent.updated_at = datetime.now(timezone.utc).isoformat()
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast_event("agent_error", f"Agent {agent.name} died unexpectedly", agent_id, agent.name)
                elif agent.pid:
                    # PID liveness check: verify the process is still alive
                    try:
                        os.kill(agent.pid, 0)
                    except ProcessLookupError:
                        # PID is dead but tmux session still exists
                        log.warning(f"Agent {agent_id} ({agent.name}) PID {agent.pid} is dead but tmux session alive")
                        agent.set_status("error")
                        agent.error_message = f"Agent process (PID {agent.pid}) died unexpectedly"
                        agent._error_entered_at = time.monotonic()
                        agent.updated_at = datetime.now(timezone.utc).isoformat()
                        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                        await hub.broadcast_event("agent_error", f"Agent {agent.name} process died (PID {agent.pid})", agent_id, agent.name)
                    except PermissionError:
                        pass  # Process exists but we can't signal it — that's fine

                # -- Idle agent reaping --
                if agent.status in ("idle", "complete") and agent.last_output_time != 0.0:
                    idle_duration = time.monotonic() - agent.last_output_time
                    idle_ttl = app["config"].idle_agent_ttl
                    if idle_ttl > 0 and idle_duration > idle_ttl:
                        log.info(f"Reaping idle agent {agent_id} ({agent.name}) — idle for {int(idle_duration)}s")
                        # Archive to history
                        db: Database = app["db"]
                        try:
                            await db.save_agent(agent)
                        except Exception as e:
                            log.warning(f"Failed to archive agent {agent_id} before reaping: {e}")
                        name = agent.name
                        await manager.kill(agent_id)
                        await hub.broadcast({"type": "agent_removed", "agent_id": agent_id, "reason": "idle_timeout"})
                        await hub.broadcast_event("agent_reaped", f"Agent {name} reaped after {int(idle_duration)}s idle", agent_id, name)

                # -- Context exhaustion auto-snapshot + warning --
                if agent.context_pct >= 0.92 and agent.status in ("working", "planning", "reading"):
                    if not agent._context_exhaustion_warned:
                        agent._context_exhaustion_warned = True
                        log.warning(f"Agent {agent_id} ({agent.name}) context at {agent.context_pct:.0%} — creating snapshot")
                        try:
                            agent.create_snapshot(trigger="context_warning")
                        except Exception as e:
                            log.warning(f"Failed to create context exhaustion snapshot: {e}")
                        await hub.broadcast_event(
                            "agent_context_warning",
                            f"Agent {agent.name} context window at {agent.context_pct:.0%} — approaching limit",
                            agent_id, agent.name,
                        )

                # -- Context exhaustion auto-pause (cascade prevention) --
                ctx_pause_threshold = app["config"].context_auto_pause_threshold
                if agent.context_pct >= ctx_pause_threshold and agent.status in ("working", "planning", "reading"):
                    if not agent._context_auto_paused:
                        agent._context_auto_paused = True
                        log.warning(f"Agent {agent_id} ({agent.name}) context at {agent.context_pct:.0%} — auto-pausing to prevent exhaustion crash")
                        try:
                            await manager.pause(agent_id)
                            agent.create_snapshot(trigger="context_auto_pause")
                            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                            await hub.broadcast_event(
                                "agent_context_auto_paused",
                                f"Agent {agent.name} auto-paused at {agent.context_pct:.0%} context — prevents crash cascade",
                                agent_id, agent.name,
                            )
                        except Exception as cp_err:
                            log.warning(f"Failed to auto-pause agent {agent_id} for context: {cp_err}")

                # -- Workflow stage stall detection --
                if agent.workflow_run_id and agent.status == "paused":
                    pause_duration = time.monotonic() - agent.last_output_time if agent.last_output_time != 0.0 else 0
                    if pause_duration > app["config"].stall_timeout_minutes * 60:
                        if not getattr(agent, '_workflow_stall_warned', False):
                            agent._workflow_stall_warned = True
                            await hub.broadcast_event(
                                "workflow_stage_stalled",
                                f"Workflow agent {agent.name} paused for {int(pause_duration)}s — blocking downstream",
                                agent_id, agent.name,
                            )

                if agent.workflow_run_id and agent.status == "working" and agent.last_output_time != 0.0:
                    wf_silence = time.monotonic() - agent.last_output_time
                    if wf_silence > app["config"].hung_timeout_minutes * 60:
                        if not getattr(agent, '_workflow_hung_warned', False):
                            agent._workflow_hung_warned = True
                            await hub.broadcast_event(
                                "workflow_stage_hung",
                                f"Workflow agent {agent.name} has no output for {int(wf_silence)}s — may be hung",
                                agent_id, agent.name,
                            )
        except Exception as e:
            log.error(f"Health check error: {e}")

        # Fleet-wide cost limit check
        try:
            config: Config = app["config"]
            if config.cost_budget_usd > 0 and config.cost_budget_auto_pause:
                fleet_cost = sum(a.estimated_cost_usd for a in list(manager.agents.values()))
                if fleet_cost >= config.cost_budget_usd:
                    if not app.get('_fleet_budget_warned', False):
                        app['_fleet_budget_warned'] = True
                        log.warning(f"Fleet cost ${fleet_cost:.2f} exceeds budget ${config.cost_budget_usd:.2f} — auto-pausing working agents")
                        working = [a for a in list(manager.agents.values()) if a.status in ("working", "planning", "reading")]
                        for a in working:
                            try:
                                await manager.pause(a.id)
                                await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})
                            except Exception as pe:
                                log.warning(f"Failed to auto-pause agent {a.id}: {pe}")
                        await hub.broadcast_event(
                            "fleet_budget_exceeded",
                            f"Fleet cost ${fleet_cost:.2f} exceeds budget ${config.cost_budget_usd:.2f}. {len(working)} agents auto-paused.",
                            None, None,
                        )
                else:
                    # Cost below budget — clear flag so it can trigger again
                    if app.get('_fleet_budget_warned', False):
                        app['_fleet_budget_warned'] = False
        except Exception as e:
            log.error(f"Fleet budget check error: {e}")

        # Fleet-wide system pressure response — pause lowest-health agents to relieve pressure
        try:
            pressure = manager.check_system_pressure()
            if pressure.get("cpu_pressure") or pressure.get("memory_pressure"):
                if not app.get('_fleet_pressure_warned', False):
                    app['_fleet_pressure_warned'] = True
                    reasons = []
                    if pressure.get("cpu_pressure"):
                        reasons.append(f"CPU {pressure.get('cpu_pct', 0):.0f}%")
                    if pressure.get("memory_pressure"):
                        reasons.append(f"Memory {pressure.get('memory_pct', 0):.0f}%")
                    pressure_str = ", ".join(reasons)
                    log.warning(f"System pressure detected ({pressure_str}) — pausing lowest-health working agents")
                    # Sort working agents by health_score ascending, pause bottom 25% (at least 1)
                    working = sorted(
                        [a for a in list(manager.agents.values()) if a.status in ("working", "planning", "reading")],
                        key=lambda a: a.health_score,
                    )
                    to_pause = max(1, len(working) // 4)
                    paused_names = []
                    for a in working[:to_pause]:
                        try:
                            a._pressure_paused = True
                            await manager.pause(a.id)
                            await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})
                            paused_names.append(a.name)
                        except Exception as pp_err:
                            log.warning(f"Failed to pressure-pause agent {a.id}: {pp_err}")
                    if paused_names:
                        await hub.broadcast_event(
                            "fleet_pressure_response",
                            f"System under pressure ({pressure_str}). Paused {len(paused_names)} agent(s): {', '.join(paused_names)}",
                            None, None,
                        )
            else:
                # Pressure relieved — clear flag so it can trigger again
                if app.get('_fleet_pressure_warned', False):
                    app['_fleet_pressure_warned'] = False
                    # Resume pressure-paused agents
                    for a in list(manager.agents.values()):
                        if a._pressure_paused and a.status == "paused":
                            try:
                                a._pressure_paused = False
                                await manager.resume(a.id)
                                await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})
                            except Exception as e:
                                log.warning(f"Failed to resume agent after pressure relief: {e}")
        except Exception as e:
            log.debug(f"Fleet pressure check error: {e}")

        # Auto-spawn from task queue when slots are available
        try:
            config: Config = app["config"]
            queue_max = _effective_max_agents(app)
            active_count = sum(1 for a in list(manager.agents.values()) if a.status not in ("idle", "complete"))
            while manager.task_queue and active_count < queue_max:
                queued = manager.task_queue.pop(0)
                try:
                    agent = await manager.spawn(
                        role=queued.role,
                        name=queued.name,
                        working_dir=queued.working_dir,
                        task=queued.task,
                        backend=queued.backend or config.default_backend,
                        plan_mode=queued.plan_mode,
                    )
                    if queued.project_id:
                        agent.project_id = queued.project_id
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                    await hub.broadcast_event(
                        "agent_spawned",
                        f"Auto-spawned from queue: {agent.name}",
                        agent.id, agent.name,
                    )
                    await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
                    active_count += 1
                    log.info(f"Auto-spawned queued task: {queued.name} (role={queued.role})")
                except Exception as e:
                    log.warning(f"Failed to auto-spawn queued task {queued.name}: {e}")
        except Exception as e:
            log.debug(f"Queue auto-spawn check error: {e}")

        # Workflow stage timeout enforcement
        try:
            timed_out = manager.check_stage_timeouts()
            for wf_run, agent_id, agent_name in timed_out:
                log.warning(f"Workflow stage timeout: agent {agent_name} ({agent_id}) exceeded {wf_run.stage_timeout_sec}s in workflow '{wf_run.workflow_name}'")
                # Kill the timed-out agent and treat as failed with skip
                wf_run.running_ids.discard(agent_id)
                wf_run.stage_started_at.pop(agent_id, None)
                wf_run.completed_ids.add(agent_id)  # Treat as completed so downstream can proceed
                agent = manager.agents.get(agent_id)
                if agent:
                    try:
                        await manager.kill(agent_id)
                    except Exception as e:
                        log.warning(f"Failed to kill agent on workflow timeout: {e}")
                await hub.broadcast_event(
                    "workflow_stage_timeout",
                    f"Workflow '{wf_run.workflow_name}': agent {agent_name} timed out after {int(wf_run.stage_timeout_sec)}s — skipping",
                    agent_id, agent_name,
                    {"workflow_run_id": wf_run.id},
                )
                # Resolve next stages
                await manager.resolve_workflow_deps(wf_run, hub)
        except Exception as e:
            log.debug(f"Workflow timeout check error: {e}")

        await asyncio.sleep(5.0)


async def memory_watchdog_loop(app: web.Application) -> None:
    """Check per-agent memory usage, warn/kill if over limit. Also cleans old archive rows."""
    manager: AgentManager = app["agent_manager"]
    hub: WebSocketHub = app["ws_hub"]
    limit = app["config"].memory_limit_mb
    warn_threshold = limit * 0.75
    pause_threshold = limit * app["config"].agent_memory_pause_pct
    _cleanup_counter = 0

    while True:
        try:
            for agent_id, agent in list(manager.agents.items()):
                if agent.memory_mb > limit:
                    log.warning(f"Agent {agent_id} exceeded memory limit ({agent.memory_mb}MB > {limit}MB), killing")
                    name = agent.name
                    await manager.kill(agent_id)
                    await hub.broadcast({"type": "agent_removed", "agent_id": agent_id})
                    await hub.broadcast_event("agent_killed", f"Agent {name} killed: memory limit exceeded ({agent.memory_mb}MB)", agent_id, name)
                elif agent.memory_mb > pause_threshold and agent.status in ("working", "planning", "reading"):
                    # Graduated response: pause before kill to prevent cascade
                    if not agent._memory_pause_warned:
                        agent._memory_pause_warned = True
                        log.warning(f"Agent {agent_id} memory at {agent.memory_mb}MB ({pause_threshold}MB pause threshold) — auto-pausing")
                        try:
                            await manager.pause(agent_id)
                            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                            await hub.broadcast_event(
                                "agent_memory_paused",
                                f"Agent {agent.name} auto-paused at {agent.memory_mb}MB (limit: {limit}MB) — prevent cascade",
                                agent_id, agent.name,
                            )
                        except Exception as mp_err:
                            log.warning(f"Failed to memory-pause agent {agent_id}: {mp_err}")
                elif agent.memory_mb > warn_threshold:
                    log.warning(f"Agent {agent_id} memory warning: {agent.memory_mb}MB / {limit}MB")
        except Exception as e:
            log.error(f"Memory watchdog error: {e}")

        # Archive cleanup: every ~180 iterations (30 min at 10s interval)
        _cleanup_counter += 1
        if _cleanup_counter >= 180:
            _cleanup_counter = 0
            try:
                db: Database = app["db"]
                if app.get("db_available", True) and db._db:
                    cfg: Config = app["config"]
                    retention_hours = getattr(cfg, 'archive_retention_hours', 24) or 24
                    # Delete archive rows older than retention window for agents no longer running
                    active_ids = set(manager.agents.keys())
                    async with db._db.execute(
                        "SELECT DISTINCT agent_id FROM agent_output_archive WHERE created_at < datetime('now', ?)",
                        (f"-{retention_hours} hours",),
                    ) as cursor:
                        old_agents = [row[0] async for row in cursor]
                    for aid in old_agents:
                        if aid not in active_ids:
                            await db._db.execute("DELETE FROM agent_output_archive WHERE agent_id = ?", (aid,))
                    await db._safe_commit()
                    if old_agents:
                        cleaned = [a for a in old_agents if a not in active_ids]
                        if cleaned:
                            log.info(f"Archive cleanup: removed output for {len(cleaned)} old agents")
            except Exception as e:
                log.warning(f"Archive cleanup error: {e}")

        await asyncio.sleep(10.0)


# ── Meta-Agent Intelligence Loop ──

async def meta_agent_loop(app: web.Application) -> None:
    """5th supervised background task: cross-agent analysis via Claude API.
    Runs on configurable interval (default 30s). Detects file conflicts, stuck
    agents, handoff opportunities, and anomalies.
    """
    config: Config = app["config"]
    interval = config.llm_meta_interval

    while True:
        await asyncio.sleep(interval)

        client: IntelligenceClient | None = app.get("intelligence")
        if not client or not client.available:
            continue

        manager: AgentManager = app["agent_manager"]
        agents = list(manager.agents.values())
        if len(agents) < 2:
            continue

        # Hash state to skip re-analysis when nothing changed
        state_parts = [f"{a.id}:{a.status}:{a.summary[:30]}" for a in agents]
        state_hash = str(hash(tuple(state_parts)))
        if state_hash == app.get("_meta_state_hash", ""):
            continue
        app["_meta_state_hash"] = state_hash

        try:
            insights = await client.analyze_fleet(
                agents, app.get("intelligence_insights", [])
            )
            if insights:
                existing: list[AgentInsight] = app.get("intelligence_insights", [])
                existing.extend(insights)
                # Cap at 100 insights
                if len(existing) > 100:
                    app["intelligence_insights"] = existing[-100:]

                hub: WebSocketHub = app["ws_hub"]
                for insight in insights:
                    await hub.broadcast({
                        "type": "intelligence_alert",
                        "insight": insight.to_dict(),
                    })
                log.info(f"Meta-agent generated {len(insights)} insight(s)")
        except Exception as e:
            log.debug(f"Meta-agent analysis error: {e}")


# ─────────────────────────────────────────────
# Section 10: Application Setup & Main
# ─────────────────────────────────────────────

async def _supervised_task(name: str, coro_fn, app: web.Application) -> None:
    """Supervisor wrapper: restarts a background task if it crashes."""
    restart_count = 0
    while True:
        try:
            await coro_fn(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            restart_count += 1
            log.error(f"Background task '{name}' crashed (restart #{restart_count}): {e}")
            # Track health for /api/system
            task_health = app.setdefault("bg_task_health", {})
            task_health[name] = {
                "restarts": restart_count,
                "last_crash": datetime.now(timezone.utc).isoformat(),
                "last_error": str(e)[:200],
            }
            # Broadcast crash event to connected clients
            hub: WebSocketHub | None = app.get("ws_hub")
            if hub:
                try:
                    await hub.broadcast({
                        "type": "event",
                        "event": "bg_task_crash",
                        "task": name,
                        "restart": restart_count,
                        "error": str(e)[:200],
                    })
                except Exception as e:
                    log.debug(f"Broadcast failure during task restart: {e}")
            await asyncio.sleep(min(5 * restart_count, 30))  # back off


async def archive_cleanup_loop(app: web.Application) -> None:
    """Periodically clean up old archived agent history (every 1hr, 48hr retention)."""
    while True:
        await asyncio.sleep(3600)  # Run every hour
        db: Database = app["db"]
        if not app.get("db_available", True):
            continue
        try:
            deleted = await db.cleanup_old_archives(retention_hours=48)
            if deleted > 0:
                log.info(f"Archive cleanup: removed {deleted} old records")
        except Exception as e:
            log.warning(f"Archive cleanup failed: {e}")


async def start_background_tasks(app: web.Application) -> None:
    # Initialize database with retry
    db: Database = app["db"]
    try:
        await db.init()
    except Exception as e:
        log.error(f"Database init failed: {e}, retrying...")
        await asyncio.sleep(1)
        try:
            await db.init()
        except Exception as e2:
            log.error(f"Database init failed on retry: {e2} — running in degraded mode (no persistence)")
            app["db_available"] = False

    app["db_ready"] = True

    # Load license from DB (org) or config (YAML)
    config: Config = app["config"]
    manager: AgentManager = app["agent_manager"]
    license_key = config.license_key
    if app.get("db_available", True):
        try:
            # Try to load from first org in DB
            async with db._db.execute("SELECT license_key FROM organizations WHERE license_key != '' LIMIT 1") as cur:
                row = await cur.fetchone()
                if row and row["license_key"]:
                    license_key = row["license_key"]
        except Exception:
            pass
    if license_key:
        lic = validate_license(license_key)
        app["license"] = lic
        manager.license = lic
        log.info(f"License loaded: tier={lic.tier}, max_agents={lic.max_agents}, expires={lic.expires_at or 'never'}")
    else:
        log.info("No license key found — running in Community mode (5 agents max)")

    app["bg_task_health"] = {}
    bg_tasks = [
        asyncio.create_task(_supervised_task("output_capture", output_capture_loop, app)),
        asyncio.create_task(_supervised_task("metrics", metrics_loop, app)),
        asyncio.create_task(_supervised_task("health_check", health_check_loop, app)),
        asyncio.create_task(_supervised_task("memory_watchdog", memory_watchdog_loop, app)),
        asyncio.create_task(_supervised_task("archive_cleanup", archive_cleanup_loop, app)),
    ]
    # Meta-agent intelligence task (only if intelligence client is available)
    intel = app.get("intelligence")
    if intel and intel.available:
        bg_tasks.append(asyncio.create_task(_supervised_task("meta_agent", meta_agent_loop, app)))
    app["bg_tasks"] = bg_tasks
    log.info("Background tasks started")


async def cleanup_background_tasks(app: web.Application) -> None:
    for task in app.get("bg_tasks", []):
        task.cancel()
    for task in app.get("bg_tasks", []):
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as e:
            log.warning(f"Background task cleanup error: {e}")

    # Archive remaining agents to history
    db: Database = app["db"]
    manager: AgentManager = app["agent_manager"]
    if app.get("db_available", True):
        for agent in list(manager.agents.values()):
            try:
                await db.save_agent(agent)
            except Exception as e:
                log.warning(f"Shutdown: failed to archive agent {agent.id} ({agent.name}): {e}")

    # Close intelligence client
    intel: IntelligenceClient | None = app.get("intelligence")
    if intel:
        await intel.close()

    # Close database
    await db.close()

    # Clean up all tmux sessions
    manager.cleanup_all()
    log.info("Cleanup complete")
