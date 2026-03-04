#!/usr/bin/env python3
"""
Ashlr AO — Agent Orchestration Server

aiohttp server that manages AI coding agents via tmux,
serves the web dashboard, and provides REST + WebSocket APIs.
"""

# ─────────────────────────────────────────────
# Section 1: Imports
# ─────────────────────────────────────────────

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
# aiohttp_cors removed — CORS handled natively in security_headers_middleware
import psutil
import yaml

# Re-export from constants module (backward compat)
from ashlr_ao.constants import (  # noqa: F401
    ASHLR_DIR,
    LOG_COLORS,
    RESET,
    ColoredFormatter,
    setup_logging,
    log,
    _ANSI_ESCAPE_RE,
    _strip_ansi,
    _SECRET_PATTERNS,
    redact_secrets,
    print_banner,
    check_dependencies,
)


# Re-export from config module (backward compat)
from ashlr_ao.config import (  # noqa: F401
    DEFAULT_CONFIG,
    deep_merge,
    Config,
    load_config,
)

# Re-export from licensing module (backward compat)
from ashlr_ao.licensing import (  # noqa: F401
    PRO_FEATURES,
    LICENSE_PUBLIC_KEY_PEM,
    License,
    COMMUNITY_LICENSE,
    validate_license,
    _effective_max_agents,
    _check_feature,
)

# Re-export from backends module (backward compat)
from ashlr_ao.backends import (  # noqa: F401
    BackendConfig,
    KNOWN_BACKENDS,
)

# Re-export from roles module (backward compat)
from ashlr_ao.roles import (  # noqa: F401
    Role,
    BUILTIN_ROLES,
)

# Re-export from extensions module (backward compat)
from ashlr_ao.extensions import (  # noqa: F401
    SkillInfo,
    MCPServerInfo,
    PluginInfo,
    ExtensionScanner,
)

# Re-export from intelligence module (backward compat)
from ashlr_ao.intelligence import (  # noqa: F401
    OutputIntelligenceParser,
    _intelligence_parser,
    _alert_throttle,
    IntelligenceClient,
    PHASE_PATTERNS,
    PHASE_PROGRESS,
    detect_phase,
    estimate_progress,
    calculate_health_score,
)

# Re-export from status module (backward compat)
from ashlr_ao.status import (  # noqa: F401
    STATUS_PATTERNS,
    WAITING_LINE_PATTERNS,
    FOLLOWUP_SUGGESTIONS,
    _suggest_followup,
    parse_agent_status,
    _extract_question,
    _FILE_PATH_RE,
    _TEST_RESULT_RE,
    _COVERAGE_RE,
    _FILES_PROGRESS_RE,
    _ACTION_PATTERNS,
    _INTENT_RE,
    _GIT_COMMIT_RE,
    extract_summary,
)

# Re-export from manager module (backward compat)
from ashlr_ao.manager import AgentManager  # noqa: F401

# Re-export from auth module (backward compat)
from ashlr_ao.auth import (  # noqa: F401
    _check_agent_ownership,
    _AUTH_PUBLIC_ROUTES,
    auth_middleware,
    _extract_session_cookie,
    _make_slug,
    _set_session_cookie,
    auth_status,
    auth_register,
    auth_login,
    auth_logout,
    auth_me,
    auth_invite,
    auth_team,
    verify_auth,
)

# Re-export from database module (backward compat)
from ashlr_ao.database import Database  # noqa: F401

# Re-export from websocket module (backward compat)
from ashlr_ao.websocket import (  # noqa: F401
    WebSocketHub,
    collect_system_metrics,
)

# Re-export from background module (backward compat)
from ashlr_ao.background import (  # noqa: F401
    output_capture_loop,
    metrics_loop,
    health_check_loop,
    memory_watchdog_loop,
    meta_agent_loop,
    archive_cleanup_loop,
    _supervised_task,
    start_background_tasks,
    cleanup_background_tasks,
)

# Re-export from middleware module (backward compat)
from ashlr_ao.middleware import (  # noqa: F401
    RateLimiter,
    _get_client_ip,
    _RATE_LIMIT_TIERS,
    _get_rate_tier,
    rate_limit_middleware,
    request_logging_middleware,
    compression_middleware,
    security_headers_middleware,
)

# Re-export from models module (backward compat)
from ashlr_ao.models import (  # noqa: F401
    OutputSnapshot,
    Organization,
    User,
    Project,
    Agent,
    SystemMetrics,
    ToolInvocation,
    FileOperation,
    GitOperation,
    AgentTestResult,
    AgentInsight,
    QueuedTask,
    ParsedIntent,
    WorkflowRun,
    calculate_efficiency_score,
)


# Data models moved to ashlr_ao.models — re-exported above


# AgentManager moved to ashlr_ao.manager — re-exported above


# Concurrency guard for config file writes
_config_write_lock = asyncio.Lock()


# ─────────────────────────────────────────────
# Section 8: REST API Handlers
# ─────────────────────────────────────────────

# Auth middleware and handlers moved to ashlr_ao.auth — re-exported above


async def serve_dashboard(request: web.Request) -> web.FileResponse:
    dashboard_path = Path(__file__).parent / "dashboard.html"
    if not dashboard_path.exists():
        return web.Response(text="Dashboard not found.", status=404)
    return web.FileResponse(dashboard_path)


async def serve_logo(request: web.Request) -> web.Response:
    logo_path = Path(__file__).parent / "logo.png"
    if not logo_path.exists():
        return web.Response(text="Logo not found", status=404)
    return web.FileResponse(logo_path, headers={"Cache-Control": "public, max-age=86400"})


async def list_agents(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agents_list = list(manager.agents.values())

    # Optional query filters
    branch_filter = request.query.get("branch")
    project_filter = request.query.get("project_id")
    status_filter = request.query.get("status")

    if branch_filter:
        agents_list = [a for a in agents_list if a.git_branch == branch_filter]
    if project_filter:
        agents_list = [a for a in agents_list if a.project_id == project_filter]
    if status_filter:
        agents_list = [a for a in agents_list if a.status == status_filter]

    return web.json_response([a.to_dict() for a in agents_list])


def _check_rate(request: web.Request, cost: float = 1.0, rate: float = 5.0, burst: float = 10.0) -> web.Response | None:
    """Check rate limit. Returns a 429 response if exceeded, None if OK."""
    rl: RateLimiter = request.app["rate_limiter"]
    ip = _get_client_ip(request)
    allowed, retry_after = rl.check(ip, cost, rate, burst)
    if not allowed:
        return web.json_response(
            {"error": "Too many requests", "retry_after": round(retry_after, 1)},
            status=429,
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    return None


async def spawn_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    if r := _check_rate(request, cost=3, rate=1.0, burst=5):
        return r
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # REST-level input validation (before calling manager.spawn)
    name = data.get("name")
    if name is not None:
        if not isinstance(name, str):
            return web.json_response({"error": "name must be a string"}, status=400)
        name = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
        if not name:
            return web.json_response({"error": "name cannot be empty"}, status=400)

    task = data.get("task", "")
    if not isinstance(task, str):
        return web.json_response({"error": "task must be a string"}, status=400)
    if not task.strip():
        return web.json_response({"error": "task is required"}, status=400)
    if len(task) > 10000:
        return web.json_response({"error": "task exceeds 10000 character limit"}, status=400)

    role = data.get("role", request.app["config"].default_role)
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)

    backend = data.get("backend", "claude-code")
    if not isinstance(backend, str):
        return web.json_response({"error": "backend must be a string"}, status=400)
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if backend not in valid_backends:
        return web.json_response({"error": f"Unknown backend '{backend}'. Available: {', '.join(sorted(valid_backends))}"}, status=400)

    working_dir = data.get("working_dir")
    if working_dir is not None and not isinstance(working_dir, str):
        return web.json_response({"error": "working_dir must be a string"}, status=400)
    if working_dir:
        expanded = os.path.expanduser(working_dir)
        if not os.path.isdir(expanded):
            return web.json_response({"error": f"Working directory not found: {working_dir}"}, status=400)

    # Optional orchestration fields
    model_sel = data.get("model")
    if model_sel is not None and not isinstance(model_sel, str):
        return web.json_response({"error": "model must be a string"}, status=400)
    tools_sel = data.get("tools")
    if tools_sel is not None and not isinstance(tools_sel, list):
        return web.json_response({"error": "tools must be a list of strings"}, status=400)
    if tools_sel:
        import re as _re
        _TOOL_NAME_RE = _re.compile(r'^[a-zA-Z0-9_-]+$')
        for t in tools_sel:
            if not isinstance(t, str) or not _TOOL_NAME_RE.match(t):
                return web.json_response({"error": f"Invalid tool name: {t!r}. Tool names must be alphanumeric, hyphens, or underscores."}, status=400)
    system_prompt_extra = data.get("system_prompt_extra")
    if system_prompt_extra is not None and not isinstance(system_prompt_extra, str):
        return web.json_response({"error": "system_prompt_extra must be a string"}, status=400)
    if system_prompt_extra is not None and len(system_prompt_extra) > 5000:
        return web.json_response({"error": "system_prompt_extra exceeds 5000 character limit"}, status=400)
    resume_session = data.get("resume_session")

    plan_mode = data.get("plan_mode", False)
    if not isinstance(plan_mode, bool):
        return web.json_response({"error": "plan_mode must be a boolean"}, status=400)

    project_id = data.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        return web.json_response({"error": "project_id must be a string"}, status=400)

    # Project-aware defaults: fill unspecified fields from project config
    _project_for_task = None
    if project_id:
        db_for_defaults: Database = request.app["db"]
        _project_for_task = await db_for_defaults.get_project(project_id)
        if _project_for_task:
            if not working_dir and _project_for_task.get("path"):
                working_dir = _project_for_task["path"]
            if "backend" not in data and _project_for_task.get("default_backend"):
                backend = _project_for_task["default_backend"]
            if "model" not in data and _project_for_task.get("default_model"):
                model_sel = _project_for_task["default_model"]
            if "role" not in data and _project_for_task.get("default_role"):
                proj_role = _project_for_task["default_role"]
                if proj_role in BUILTIN_ROLES:
                    role = proj_role

    try:
        agent = await manager.spawn(
            role=role,
            name=name,
            working_dir=working_dir,
            task=task,
            plan_mode=plan_mode,
            backend=backend,
            model=model_sel,
            tools=tools_sel,
            system_prompt_extra=system_prompt_extra,
            resume_session=resume_session,
        )

        # Assign project after spawn
        if project_id:
            agent.project_id = project_id
        else:
            # Auto-assign project based on working directory match
            db: Database = request.app["db"]
            projects = await db.get_projects()
            best_match = None
            best_len = 0
            for proj in projects:
                proj_path = os.path.abspath(os.path.expanduser(proj.get("path", "")))
                if agent.working_dir.startswith(proj_path) and len(proj_path) > best_len:
                    best_match = proj
                    best_len = len(proj_path)
            if best_match:
                agent.project_id = best_match["id"]
                _project_for_task = best_match

        # Record recent task on the project
        if agent.project_id and task:
            try:
                db_rt: Database = request.app["db"]
                await db_rt.add_recent_task(agent.project_id, task, role, backend)
            except Exception:
                pass  # Non-critical

        # Set owner from authenticated user
        user = request.get("user")
        if user:
            agent.owner_id = user.id
            agent.owner_name = user.display_name

        # Broadcast to WebSocket clients (skip ghost broadcast on spawn failure)
        hub: WebSocketHub = request.app["ws_hub"]
        if agent.status != "error":
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
            await hub.broadcast_event("agent_spawned", f"Agent {agent.name} spawned", agent.id, agent.name)

        return web.json_response(agent.to_dict(), status=201)
    except ValueError as e:
        return web.json_response({"error": redact_secrets(str(e))}, status=400)


async def get_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    return web.json_response(agent.to_dict_full())


async def delete_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

    # Archive to history before killing
    try:
        await db.save_agent(agent)
    except Exception as e:
        log.warning(f"Failed to archive agent {agent_id}: {e}")

    name = agent.name
    success = await manager.kill(agent_id)
    if success:
        await hub.broadcast({"type": "agent_removed", "agent_id": agent_id})
        await hub.broadcast_event("agent_killed", f"Agent {name} killed", agent_id, name)
        return web.json_response({"status": "killed"})
    return web.json_response({"error": f"Failed to kill agent '{name}' — tmux session may have already terminated"}, status=500)


async def get_agent_output_history(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/output — full output history (in-memory + archived)."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)

    try:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 2000))
    except ValueError:
        offset, limit = 0, 2000
    limit = max(1, min(limit, 10000))

    lines: list[str] = []

    # Get archived lines from DB
    archived, total_archived = await db.get_archived_output(agent_id, offset=offset, limit=limit)
    lines.extend(archived)

    # Get in-memory lines from active agent
    if agent:
        mem_lines = list(agent.output_lines)
        remaining = limit - len(lines)
        if remaining > 0:
            lines.extend(mem_lines[:remaining])

    return web.json_response({
        "agent_id": agent_id,
        "lines": lines,
        "total_archived": total_archived,
        "total_memory": len(agent.output_lines) if agent else 0,
        "offset": offset,
        "limit": limit,
    })


async def send_to_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    if r := _check_rate(request, cost=1, rate=5.0, burst=15):
        return r
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    message = data.get("message", "")
    if not isinstance(message, str) or not message:
        return web.json_response({"error": "No message provided (must be a string)"}, status=400)
    if len(message) > 50_000:
        return web.json_response({"error": "Message too long (max 50,000 chars)"}, status=400)

    success = await manager.send_message(agent_id, message)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "sent"})
    agent_name = agent.name if agent else agent_id
    return web.json_response({"error": f"Failed to send message to '{agent_name}' — agent may have terminated"}, status=500)


async def update_agent_notes(request: web.Request) -> web.Response:
    """PUT /api/agents/{id}/notes — update agent notes."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        return web.json_response({"error": "Notes must be a string"}, status=400)
    if len(notes) > 10000:
        return web.json_response({"error": "Notes too long (max 10,000 chars)"}, status=400)
    agent.notes = notes
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
    return web.json_response({"status": "updated", "notes": agent.notes})


async def update_agent_tags(request: web.Request) -> web.Response:
    """PUT /api/agents/{id}/tags — update agent tags."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return web.json_response({"error": "Tags must be a list of strings"}, status=400)
    if len(tags) > 20:
        return web.json_response({"error": "Maximum 20 tags"}, status=400)
    # Sanitize: strip whitespace, lowercase, remove empty, deduplicate
    clean_tags = list(dict.fromkeys(t.strip().lower()[:30] for t in tags if t.strip()))
    agent.tags = clean_tags
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
    return web.json_response({"status": "updated", "tags": agent.tags})


async def add_agent_bookmark(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/bookmarks — add a bookmark to agent output."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    line_idx = data.get("line", 0)
    text = data.get("text", "")
    label = data.get("label", "")
    color = data.get("color", "accent")
    if not isinstance(line_idx, int) or not isinstance(text, str):
        return web.json_response({"error": "Invalid bookmark data"}, status=400)
    if len(agent.bookmarks) >= 100:
        return web.json_response({"error": "Maximum 100 bookmarks per agent"}, status=400)
    bm_id = uuid.uuid4().hex[:6]
    bookmark = {
        "id": bm_id,
        "line": line_idx,
        "text": text[:200],
        "label": label[:100] if label else "",
        "color": color,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    agent.bookmarks.append(bookmark)
    # Persist to SQLite
    try:
        await db.add_bookmark(agent_id, line_idx, text[:200], label[:100], color)
    except Exception as e:
        log.debug(f"Failed to persist bookmark to DB: {e}")
    return web.json_response({"status": "added", "bookmark": bookmark})


async def delete_agent_bookmark(request: web.Request) -> web.Response:
    """DELETE /api/agents/{id}/bookmarks/{bid} — remove a bookmark."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    bookmark_id = request.match_info["bid"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err
    before = len(agent.bookmarks)
    agent.bookmarks = [b for b in agent.bookmarks if b.get("id") != bookmark_id]
    if len(agent.bookmarks) == before:
        return web.json_response({"error": "Bookmark not found"}, status=404)
    return web.json_response({"status": "deleted"})


async def list_agent_bookmarks(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/bookmarks — list agent bookmarks (memory + SQLite)."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    # If in-memory is empty, try to load from SQLite
    if not agent.bookmarks:
        try:
            saved = await db.get_bookmarks(agent_id)
            for bm in saved:
                agent.bookmarks.append({
                    "id": str(bm.get("id", "")),
                    "line": bm.get("line_index", 0),
                    "text": bm.get("line_text", ""),
                    "label": bm.get("annotation", ""),
                    "color": bm.get("color", "accent"),
                    "created_at": bm.get("created_at", ""),
                })
        except Exception as e:
            log.error(f"Failed to load bookmarks: {e}")
    return web.json_response({"bookmarks": agent.bookmarks})


async def pause_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

    success = await manager.pause(agent_id)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "paused"})
    return web.json_response({"error": "Failed to pause"}, status=500)


async def resume_agent(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

    try:
        data = await request.json()
        message = data.get("message")
        if message is not None and not isinstance(message, str):
            return web.json_response({"error": "Message must be a string"}, status=400)
    except Exception:
        message = None

    success = await manager.resume(agent_id, message)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "resumed"})
    return web.json_response({"error": "Failed to resume"}, status=500)


async def restart_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/restart — Manually restart an agent."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    if r := _check_rate(request, cost=2, rate=0.5, burst=3):
        return r
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err

    # Optional task override for retry-with-changes
    new_task: str | None = None
    if request.content_type and "json" in request.content_type:
        try:
            body = await request.json()
            new_task = body.get("task") if isinstance(body, dict) else None
            if new_task and not isinstance(new_task, str):
                return web.json_response({"error": "task must be a string"}, status=400)
            if new_task and len(new_task) > 5000:
                return web.json_response({"error": "task too long (max 5000 chars)"}, status=400)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON in request body"}, status=400)
        except Exception:
            pass  # Non-JSON content type — proceed without task override

    try:
        success = await manager.restart(agent_id, new_task=new_task)
        if success:
            restarted = manager.agents.get(agent_id)
            if restarted:
                await hub.broadcast({"type": "agent_update", "agent": restarted.to_dict()})
                msg = f"Agent {restarted.name} manually restarted (attempt {restarted.restart_count})"
                if new_task:
                    msg += " with modified task"
                await hub.broadcast_event("agent_restarted", msg, agent_id, restarted.name)
                return web.json_response({"status": "restarted", "restart_count": restarted.restart_count, "task_modified": bool(new_task)})
        return web.json_response({"error": "Restart failed"}, status=500)
    except Exception as e:
        log.error(f"Restart endpoint error for {agent_id}: {e}")
        return web.json_response({"error": redact_secrets(str(e))}, status=500)


async def system_metrics(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    metrics = await collect_system_metrics(manager)
    data = metrics.to_dict()
    # Service health indicators
    config: Config = request.app["config"]
    data["services"] = {
        "db_available": request.app.get("db_available", True),
        "llm_enabled": config.llm_enabled,
        "bg_task_health": request.app.get("bg_task_health", {}),
    }
    data["server_cwd"] = os.getcwd()
    data["uptime_sec"] = round(time.monotonic() - request.app.get("_start_time", time.monotonic()))
    return web.json_response(data)


async def fleet_analytics(request: web.Request) -> web.Response:
    """GET /api/analytics — fleet-wide performance analytics."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agents = list(manager.agents.values())

    total_cost = sum(a.estimated_cost_usd for a in agents)
    total_tokens_in = sum(a.tokens_input for a in agents)
    total_tokens_out = sum(a.tokens_output for a in agents)
    total_restarts = sum(a.restart_count for a in agents)

    status_counts = {}
    role_counts = {}
    for a in agents:
        status_counts[a.status] = status_counts.get(a.status, 0) + 1
        role_counts[a.role] = role_counts.get(a.role, 0) + 1

    # File activity across all agents
    all_files: dict[str, int] = {}
    for a in agents:
        for fop in list(a._file_operations)[-100:]:
            path = fop.file_path
            all_files[path] = all_files.get(path, 0) + 1
    top_files = sorted(all_files.items(), key=lambda x: -x[1])[:20]

    # Tool usage across all agents
    tool_counts: dict[str, int] = {}
    for a in agents:
        for inv in list(a._tool_invocations)[-200:]:
            tool_counts[inv.tool] = tool_counts.get(inv.tool, 0) + 1

    # Average health score
    health_scores = [a.health_score for a in agents if a.health_score > 0]
    avg_health = sum(health_scores) / len(health_scores) if health_scores else 0

    # Agent lifespans
    lifespans = []
    for a in agents:
        if a.created_at:
            try:
                created = datetime.fromisoformat(a.created_at.replace('Z', '+00:00'))
                age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
                lifespans.append(age_min)
            except Exception as e:
                log.debug(f"Failed to parse agent lifespan for {a.id}: {e}")
    avg_lifespan_min = sum(lifespans) / len(lifespans) if lifespans else 0

    # Historical count
    history_count = await db.get_agent_history_count()

    # Historical analytics (success rates, cost breakdowns)
    historical = await db.get_historical_analytics()

    return web.json_response({
        "total_agents": len(agents),
        "total_cost_usd": round(total_cost, 4),
        "total_tokens_input": total_tokens_in,
        "total_tokens_output": total_tokens_out,
        "total_restarts": total_restarts,
        "status_distribution": status_counts,
        "role_distribution": role_counts,
        "top_files": [{"path": p, "count": c} for p, c in top_files],
        "tool_usage": tool_counts,
        "avg_health_score": round(avg_health, 1),
        "avg_lifespan_minutes": round(avg_lifespan_min, 1),
        "historical_agents": history_count,
        "historical": historical,
    })


async def collaboration_graph(request: web.Request) -> web.Response:
    """GET /api/collaboration — graph of agent relationships for visualization."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agents = list(manager.agents.values())

    nodes = []
    for a in agents:
        nodes.append({
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "status": a.status,
            "health_score": a.health_score,
            "project_id": a.project_id,
            "file_count": len(a._file_operations),
            "tool_count": len(a._tool_invocations),
        })

    edges: list[dict] = []
    edge_set: set[tuple[str, str, str]] = set()

    # 1. Message-based edges
    if db and db._db:
        try:
            async with db._db.execute(
                "SELECT from_agent_id, to_agent_id, COUNT(*) as cnt FROM agent_messages GROUP BY from_agent_id, to_agent_id"
            ) as cur:
                async for row in cur:
                    from_id, to_id, cnt = row[0], row[1], row[2]
                    if from_id in {a.id for a in agents} and to_id in {a.id for a in agents}:
                        key = (from_id, to_id, "message")
                        if key not in edge_set:
                            edge_set.add(key)
                            edges.append({"from": from_id, "to": to_id, "type": "message", "weight": cnt})
        except Exception as e:
            log.error(f"Failed to query collaboration graph: {e}")

    # 2. Shared file edges (agents writing/reading the same files)
    file_agents: dict[str, set[str]] = {}
    for a in agents:
        for fop in list(a._file_operations)[-200:]:
            path = fop.file_path
            if path not in file_agents:
                file_agents[path] = set()
            file_agents[path].add(a.id)

    # Use dict for O(1) edge weight updates instead of scanning edges list
    edge_dict: dict[tuple, dict] = {}  # (from_id, to_id, type) → edge dict
    for path, agent_ids in file_agents.items():
        ids = list(agent_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                key = (min(ids[i], ids[j]), max(ids[i], ids[j]), "shared_file")
                if key not in edge_set:
                    edge_set.add(key)
                    edge = {"from": ids[i], "to": ids[j], "type": "shared_file", "weight": 1, "file": path.split("/")[-1]}
                    edges.append(edge)
                    edge_dict[key] = edge
                else:
                    edge_dict[key]["weight"] += 1

    # 3. Same-project edges
    project_agents: dict[str, list[str]] = {}
    for a in agents:
        if a.project_id:
            if a.project_id not in project_agents:
                project_agents[a.project_id] = []
            project_agents[a.project_id].append(a.id)

    for proj, agent_ids in project_agents.items():
        for i in range(len(agent_ids)):
            for j in range(i + 1, len(agent_ids)):
                key = (min(agent_ids[i], agent_ids[j]), max(agent_ids[i], agent_ids[j]), "project")
                if key not in edge_set:
                    edge_set.add(key)
                    edge = {"from": agent_ids[i], "to": agent_ids[j], "type": "project", "weight": 1}
                    edges.append(edge)
                    edge_dict[key] = edge

    # 4. Conflict edges (from file_activity — agents writing to same files)
    agent_id_set = {a.id for a in agents}
    for path, agent_ops in manager.file_activity.items():
        writers = [aid for aid, op in agent_ops.items() if op == "write" and aid in agent_id_set]
        for i in range(len(writers)):
            for j in range(i + 1, len(writers)):
                key = (min(writers[i], writers[j]), max(writers[i], writers[j]), "conflict")
                if key not in edge_set:
                    edge_set.add(key)
                    edges.append({"from": writers[i], "to": writers[j], "type": "conflict", "weight": 1, "file": path.split("/")[-1]})

    return web.json_response({"nodes": nodes, "edges": edges})


async def health_check(request: web.Request) -> web.Response:
    """Lightweight health check — no auth required."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    agents = manager.agents
    return web.json_response({
        "status": "ok",
        "agents": len(agents),
        "agents_active": sum(1 for a in agents.values() if a.status in ("working", "planning", "reading")),
        "agents_waiting": sum(1 for a in agents.values() if a.needs_input),
        "db_available": request.app.get("db_available", True),
        "llm_enabled": config.llm_enabled,
        "backends": {k: v.available for k, v in manager.backend_configs.items()},
    })


async def health_detailed(request: web.Request) -> web.Response:
    """GET /api/health/detailed — comprehensive server health and stats."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agents = manager.agents

    uptime_s = time.monotonic() - manager._start_time
    uptime_str = f"{int(uptime_s // 3600)}h{int((uptime_s % 3600) // 60)}m{int(uptime_s % 60)}s"

    total_cost = sum(a.estimated_cost_usd for a in agents.values())
    total_memory = sum(a.memory_mb for a in agents.values())
    total_tokens = sum((a.tokens_input or 0) + (a.tokens_output or 0) for a in agents.values())
    total_tool_invocations = sum(len(a._tool_invocations) for a in agents.values())
    total_file_operations = sum(len(a._file_operations) for a in agents.values())

    status_counts: dict[str, int] = {}
    for a in agents.values():
        status_counts[a.status] = status_counts.get(a.status, 0) + 1

    # Database info
    db = request.app.get("db")
    db_size_mb = 0.0
    if db and hasattr(db, 'db_path'):
        try:
            db_size_mb = Path(db.db_path).stat().st_size / (1024 * 1024)
        except Exception as e:
            log.debug(f"Failed to get DB file size: {e}")

    # Background task health
    bg_task_list = request.app.get("bg_tasks", [])
    bg_task_names = ["output_capture", "metrics", "health_check", "memory_watchdog", "archive_cleanup", "meta_agent"]
    bg_task_status = {}
    for i, task in enumerate(bg_task_list):
        name = bg_task_names[i] if i < len(bg_task_names) else f"task_{i}"
        bg_task_status[name] = "running" if not task.done() else "stopped"

    # Process memory (server itself)
    try:
        import psutil
        process = psutil.Process()
        server_memory_mb = process.memory_info().rss / (1024 * 1024)
    except Exception:
        server_memory_mb = 0.0

    return web.json_response({
        "status": "ok",
        "uptime": uptime_str,
        "uptime_seconds": round(uptime_s),
        "server_memory_mb": round(server_memory_mb, 1),
        "agents": {
            "active": len(agents),
            "total_spawned": manager._total_spawned,
            "total_killed": manager._total_killed,
            "total_messages_sent": manager._total_messages_sent,
            "status_breakdown": status_counts,
            "total_agent_memory_mb": round(total_memory, 1),
        },
        "costs": {
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
        },
        "intelligence": {
            "total_tool_invocations": total_tool_invocations,
            "total_file_operations": total_file_operations,
        },
        "websocket": {
            "connected_clients": len(hub.clients),
        },
        "database": {
            "available": request.app.get("db_available", True),
            "size_mb": round(db_size_mb, 2),
        },
        "background_tasks": bg_task_status,
        "llm": {
            "enabled": config.llm_enabled,
            "provider": config.llm_provider if config.llm_enabled else None,
        },
        "backends": {k: {"available": v.available, "command": v.command} for k, v in manager.backend_configs.items()},
        "config": {
            "max_concurrent": config.max_agents,
            "port": config.port,
            "cost_budget_usd": config.cost_budget_usd,
        },
    })


async def run_diagnostic(request: web.Request) -> web.Response:
    """POST /api/diagnostic — run server self-test and report capabilities."""
    results: dict[str, dict] = {}

    # 1. tmux check
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-V", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        results["tmux"] = {"ok": True, "version": stdout.decode().strip()}
    except Exception as e:
        results["tmux"] = {"ok": False, "error": redact_secrets(str(e))}

    # 2. Python version
    results["python"] = {"ok": True, "version": sys.version, "executable": sys.executable}

    # 3. Disk space
    try:
        disk = psutil.disk_usage("/")
        free_gb = disk.free / (1024**3)
        results["disk"] = {"ok": free_gb > 1.0, "free_gb": round(free_gb, 1), "pct_used": disk.percent}
    except Exception as e:
        results["disk"] = {"ok": False, "error": redact_secrets(str(e))}

    # 4. Database write/read test
    try:
        db: Database = request.app["db"]
        if db and db._db:
            await db._db.execute("CREATE TABLE IF NOT EXISTS _diagnostic_test (id INTEGER PRIMARY KEY, val TEXT)")
            await db._db.execute("INSERT OR REPLACE INTO _diagnostic_test (id, val) VALUES (1, 'ok')")
            async with db._db.execute("SELECT val FROM _diagnostic_test WHERE id = 1") as cursor:
                row = await cursor.fetchone()
            await db._db.execute("DROP TABLE IF EXISTS _diagnostic_test")
            await db._db.commit()
            results["database"] = {"ok": row and row[0] == "ok", "write_read": "pass"}
        else:
            results["database"] = {"ok": False, "error": "Database not available"}
    except Exception as e:
        results["database"] = {"ok": False, "error": redact_secrets(str(e))}

    # 5. System resources
    try:
        mem = psutil.virtual_memory()
        results["system"] = {
            "ok": True,
            "cpu_count": psutil.cpu_count(),
            "cpu_pct": psutil.cpu_percent(interval=None),
            "memory_total_gb": round(mem.total / (1024**3), 1),
            "memory_available_gb": round(mem.available / (1024**3), 1),
            "memory_pct": mem.percent,
        }
    except Exception as e:
        results["system"] = {"ok": False, "error": redact_secrets(str(e))}

    # 6. Backend availability
    manager: AgentManager = request.app["agent_manager"]
    backends = {}
    for name, bc in manager.backend_configs.items():
        backends[name] = {"available": bc.available, "command": bc.command}
    results["backends"] = {"ok": any(bc.available for bc in manager.backend_configs.values()), "backends": backends}

    all_ok = all(r.get("ok", False) for r in results.values())
    return web.json_response({
        "status": "ok" if all_ok else "degraded",
        "results": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def get_server_stats(request: web.Request) -> web.Response:
    """GET /api/stats — server uptime, request counts, agent lifecycle stats."""
    config: Config = request.app["config"]
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]

    uptime_sec = time.monotonic() - manager._start_time
    uptime_human = f"{int(uptime_sec // 3600)}h {int((uptime_sec % 3600) // 60)}m {int(uptime_sec % 60)}s"

    # Agent stats
    active = len(manager.agents)
    by_status: dict[str, int] = {}
    for agent in list(manager.agents.values()):
        by_status[agent.status] = by_status.get(agent.status, 0) + 1

    # DB size
    db_size_mb = 0.0
    try:
        if db.db_path.exists():
            db_size_mb = round(db.db_path.stat().st_size / (1024 * 1024), 2)
    except Exception as e:
        log.debug(f"Failed to get DB file size for costs: {e}")

    # Request count from logging middleware (tracked on manager)
    request_count = manager._total_api_requests

    return web.json_response({
        "uptime_sec": round(uptime_sec, 1),
        "uptime_human": uptime_human,
        "agents": {
            "active": active,
            "by_status": by_status,
            "total_spawned": manager._total_spawned,
            "total_killed": manager._total_killed,
            "total_messages_sent": manager._total_messages_sent,
        },
        "database": {
            "size_mb": db_size_mb,
            "path": str(db.db_path),
        },
        "config": {
            "max_agents": config.max_agents,
            "demo_mode": config.demo_mode,
            "llm_enabled": config.llm_enabled,
            "archive_max_rows_per_agent": config.archive_max_rows_per_agent,
            "archive_retention_hours": config.archive_retention_hours,
        },
        "request_count": request_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def validate_spawn(request: web.Request) -> web.Response:
    """POST /api/agents/validate — dry-run spawn validation.

    Validates all spawn parameters without actually creating an agent.
    Returns {valid: true, resolved: {...}} or {valid: false, errors: [...]}.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"valid": False, "errors": ["Invalid JSON body"]}, status=400)

    errors: list[str] = []
    warnings: list[str] = []
    resolved: dict[str, object] = {}
    manager: AgentManager = request.app["agent_manager"]
    config: Config = request.app["config"]

    # ── Role ──
    role = body.get("role", "general")
    if role not in BUILTIN_ROLES:
        errors.append(f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}")
    else:
        resolved["role"] = role
        resolved["role_name"] = BUILTIN_ROLES[role].name

    # ── Name ──
    name = body.get("name")
    if name:
        sanitized = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
        if not sanitized:
            errors.append("Agent name is empty after sanitization")
        else:
            resolved["name"] = sanitized
            existing_names = {a.name for a in list(manager.agents.values())}
            if sanitized in existing_names:
                warnings.append(f"Name '{sanitized}' already in use — will be suffixed with agent ID")
    else:
        resolved["name"] = "(auto-generated)"

    # ── Backend ──
    backend = body.get("backend", "claude-code")
    if not config.demo_mode:
        if backend not in config.backends:
            errors.append(f"Unknown backend '{backend}'. Available: {', '.join(config.backends.keys())}")
        else:
            bc = manager.backend_configs.get(backend)
            if bc and not bc.available:
                warnings.append(f"Backend '{backend}' is configured but CLI not found on PATH")
            resolved["backend"] = backend
    else:
        resolved["backend"] = backend

    # ── Working directory ──
    working_dir = body.get("working_dir")
    if working_dir:
        wd = os.path.abspath(os.path.expanduser(working_dir))
        if not os.path.isdir(wd):
            home_dir = str(Path.home())
            if not wd.startswith(home_dir):
                errors.append(f"Working directory does not exist and is outside home: {wd}")
            else:
                warnings.append(f"Working directory '{wd}' does not exist — will be created")
        resolved["working_dir"] = wd
    else:
        resolved["working_dir"] = os.path.abspath(os.path.expanduser(config.default_working_dir))

    # ── Task ──
    task = body.get("task", "")
    if task and len(task) > 10000:
        errors.append("Task description exceeds 10000 character limit")
    elif not task:
        warnings.append("No task provided — agent will start with no instructions")
    resolved["task_length"] = len(task)

    # ── Capacity ──
    agent_count = len(manager.agents)
    resolved["current_agents"] = agent_count
    resolved["max_agents"] = config.max_agents
    if agent_count >= config.max_agents:
        errors.append(f"Maximum agents ({config.max_agents}) already reached")

    # ── System pressure ──
    if config.spawn_pressure_block:
        pressure = manager.check_system_pressure()
        if pressure.get("cpu_pressure") or pressure.get("memory_pressure"):
            reasons = []
            if pressure.get("cpu_pressure"):
                reasons.append(f"CPU at {pressure.get('cpu_pct', 0):.0f}%")
            if pressure.get("memory_pressure"):
                reasons.append(f"Memory at {pressure.get('memory_pct', 0):.0f}%")
            errors.append(f"System under pressure ({', '.join(reasons)}), spawning blocked")
        resolved["system_pressure"] = pressure

    # ── Plan mode ──
    plan_mode = body.get("plan_mode", False)
    resolved["plan_mode"] = bool(plan_mode)

    valid = len(errors) == 0
    result: dict[str, object] = {"valid": valid, "resolved": resolved}
    if errors:
        result["errors"] = errors
    if warnings:
        result["warnings"] = warnings
    return web.json_response(result, status=200 if valid else 400)


async def list_roles(request: web.Request) -> web.Response:
    roles = {k: v.to_dict() for k, v in BUILTIN_ROLES.items()}
    return web.json_response(roles)


async def get_agent_output(request: web.Request) -> web.Response:
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 200))
    except ValueError:
        return web.json_response({"error": "offset and limit must be integers"}, status=400)

    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    all_lines = list(agent.output_lines)
    total = len(all_lines)
    sliced = all_lines[offset:offset + limit]

    return web.json_response({
        "data": sliced,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


async def get_agent_full_output(request: web.Request) -> web.Response:
    """Get full output: archive + live buffer combined."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        offset = int(request.query.get("offset", 0))
        limit = int(request.query.get("limit", 500))
    except ValueError:
        return web.json_response({"error": "offset and limit must be integers"}, status=400)

    limit = max(1, min(limit, 2000))
    offset = max(0, offset)

    # Get archived lines
    archived, archive_total = await db.get_archived_output(agent_id, offset, limit)
    live_lines = list(agent.output_lines)
    total = archive_total + len(live_lines)

    # Combine: if offset is within archive range, start from archive
    if offset < archive_total:
        remaining = limit - len(archived)
        if remaining > 0:
            combined = archived + live_lines[:remaining]
        else:
            combined = archived
    else:
        # Offset is beyond archive, read from live buffer
        live_offset = offset - archive_total
        combined = live_lines[live_offset:live_offset + limit]

    return web.json_response({
        "data": combined,
        "pagination": {"limit": limit, "offset": offset, "total": total},
        "archive_lines": archive_total,
        "live_lines": len(live_lines),
    })


async def export_agent_output(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/output/export — download agent output as plain text."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    # Collect all output: archive + live
    archived, _ = await db.get_archived_output(agent_id, 0, 100000)
    live = list(agent.output_lines)
    all_lines = archived + live

    # Strip ANSI codes for clean text
    ansi_re = re.compile(r'\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]')
    clean = [ansi_re.sub('', line) for line in all_lines]
    text = "\n".join(clean)

    filename = f"ashlr-{agent.name}-{agent_id}.log"
    return web.Response(
        text=text,
        content_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def search_agents(request: web.Request) -> web.Response:
    """GET /api/search?q=pattern&project_id=&status= — search across agent outputs with filters."""
    manager: AgentManager = request.app["agent_manager"]
    query = request.query.get("q", "").strip()
    if not query or len(query) < 2:
        return web.json_response({"error": "Query 'q' must be at least 2 characters"}, status=400)
    if len(query) > 200:
        return web.json_response({"error": "Query too long"}, status=400)

    try:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error:
        return web.json_response({"error": "Invalid search pattern"}, status=400)

    # Optional filters
    filter_project = request.query.get("project_id", "").strip() or None
    filter_status = request.query.get("status", "").strip() or None
    max_matches = int(request.query.get("limit", "20"))
    max_matches = min(max(1, max_matches), 50)

    results = []
    agents_searched = 0
    for agent in list(manager.agents.values()):
        # Apply filters
        if filter_project and agent.project_id != filter_project:
            continue
        if filter_status and agent.status != filter_status:
            continue
        agents_searched += 1
        matches = []
        lines = list(agent.output_lines)
        for i, line in enumerate(lines):
            stripped = _strip_ansi(line) if callable(_strip_ansi) else line
            if pattern.search(stripped):
                matches.append({"line": i, "text": stripped[:200]})
                if len(matches) >= max_matches:
                    break
        if matches:
            results.append({
                "agent_id": agent.id,
                "agent_name": agent.name,
                "role": agent.role,
                "status": agent.status,
                "project_id": agent.project_id,
                "match_count": len(matches),
                "matches": matches,
            })

    return web.json_response({"query": query, "results": results, "agents_searched": agents_searched})


async def get_config(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    return web.json_response(config.to_dict())


async def put_config(request: web.Request) -> web.Response:
    """Update runtime config and save to ashlr.yaml."""
    config: Config = request.app["config"]

    # Admin-only when auth is enabled
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required to update config"}, status=403)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Clamp max_agents to license ceiling
    lic: License = request.app.get("license", COMMUNITY_LICENSE)
    lic_max = lic.max_agents if lic.is_pro else COMMUNITY_LICENSE.max_agents
    if "max_agents" in data and isinstance(data["max_agents"], int):
        data["max_agents"] = min(data["max_agents"], lic_max)

    # Validation rules
    validators = {
        "max_agents": lambda v: isinstance(v, int) and 1 <= v <= 100,
        "default_role": lambda v: isinstance(v, str) and v in BUILTIN_ROLES,
        "default_working_dir": lambda v: isinstance(v, str) and len(v) > 0 and os.path.isdir(os.path.realpath(os.path.expanduser(v))),
        "output_capture_interval": lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 30.0,
        "memory_limit_mb": lambda v: isinstance(v, int) and 256 <= v <= 32768,
        "default_backend": lambda v: isinstance(v, str) and v in config.backends,
        "llm_enabled": lambda v: isinstance(v, bool),
        "llm_model": lambda v: isinstance(v, str) and len(v) > 0,
        "llm_summary_interval": lambda v: isinstance(v, (int, float)) and 3.0 <= v <= 120.0,
        "max_restarts": lambda v: isinstance(v, int),
        "voice_feedback": lambda v: isinstance(v, bool),
        "idle_agent_ttl": lambda v: isinstance(v, int) and 300 <= v <= 86400,
        "health_low_threshold": lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0,
        "health_critical_threshold": lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0,
        "stall_timeout_minutes": lambda v: isinstance(v, int) and 1 <= v <= 60,
        "hung_timeout_minutes": lambda v: isinstance(v, int) and 1 <= v <= 120,
        "cost_budget_usd": lambda v: isinstance(v, (int, float)) and v >= 0,
        "cost_budget_auto_pause": lambda v: isinstance(v, bool),
        "llm_meta_interval": lambda v: isinstance(v, (int, float)) and 5.0 <= v <= 300.0,
        "log_level": lambda v: isinstance(v, str) and v.upper() in {"DEBUG", "INFO", "WARNING", "ERROR"},
        "host": lambda v: isinstance(v, str) and len(v) > 0 and all(c.isalnum() or c in ".-:" for c in v),
        "auto_restart_on_stall": lambda v: isinstance(v, bool),
        "auto_approve_enabled": lambda v: isinstance(v, bool),
        "auto_approve_patterns": lambda v: isinstance(v, list) and all(isinstance(p, str) for p in v),
        "auto_pause_on_critical_health": lambda v: isinstance(v, bool),
        "file_lock_enforcement": lambda v: isinstance(v, bool),
    }

    # Clamp max_restarts to [1, 10] range
    if "max_restarts" in data and isinstance(data["max_restarts"], int):
        data["max_restarts"] = max(1, min(10, data["max_restarts"]))

    errors = []
    for key, value in data.items():
        if key in validators and not validators[key](value):
            errors.append(f"Invalid value for {key}: {value}")

    # Validate alert_patterns compile as regex
    if "alert_patterns" in data and isinstance(data["alert_patterns"], list):
        for i, ap in enumerate(data["alert_patterns"]):
            if isinstance(ap, dict) and "pattern" in ap:
                try:
                    re.compile(ap["pattern"])
                except re.error as e:
                    errors.append(f"Invalid regex in alert_patterns[{i}]: {e}")

    # Validate auto_approve_patterns compile as regex
    if "auto_approve_patterns" in data and isinstance(data["auto_approve_patterns"], list):
        for i, pat in enumerate(data["auto_approve_patterns"]):
            if isinstance(pat, str):
                try:
                    re.compile(pat)
                except re.error as e:
                    errors.append(f"Invalid regex in auto_approve_patterns[{i}]: {e}")

    if errors:
        return web.json_response({"error": "; ".join(errors)}, status=400)

    allowed_keys = set(validators.keys())

    # Build YAML-safe update dict
    yaml_update = {}
    agents_keys = {"max_agents": "max_concurrent", "default_role": "default_role",
                   "default_working_dir": "default_working_dir",
                   "output_capture_interval": "output_capture_interval_sec",
                   "memory_limit_mb": "memory_limit_mb", "default_backend": "default_backend"}
    llm_keys = {"llm_enabled": "enabled", "llm_model": "model", "llm_summary_interval": "summary_interval_sec", "llm_meta_interval": "meta_interval_sec"}
    voice_keys = {"voice_feedback": "feedback_sounds"}
    alert_keys = {
        "idle_agent_ttl": "idle_ttl_seconds",
        "health_low_threshold": "health_low_threshold",
        "health_critical_threshold": "health_critical_threshold",
        "stall_timeout_minutes": "stall_timeout_minutes",
        "hung_timeout_minutes": "hung_timeout_minutes",
        "cost_budget_usd": "cost_budget_usd",
        "cost_budget_auto_pause": "cost_budget_auto_pause",
    }
    autopilot_keys = {
        "auto_restart_on_stall": "auto_restart_on_stall",
        "auto_approve_enabled": "auto_approve_enabled",
        "auto_approve_patterns": "auto_approve_patterns",
        "auto_pause_on_critical_health": "auto_pause_on_critical_health",
        "file_lock_enforcement": "file_lock_enforcement",
    }
    for key, value in data.items():
        if key not in allowed_keys:
            continue
        if key in agents_keys:
            yaml_update.setdefault("agents", {})[agents_keys[key]] = value
        elif key in llm_keys:
            yaml_update.setdefault("llm", {})[llm_keys[key]] = value
        elif key in voice_keys:
            yaml_update.setdefault("voice", {})[voice_keys[key]] = value
        elif key in alert_keys:
            yaml_update.setdefault("alerts", {})[alert_keys[key]] = value
        elif key in autopilot_keys:
            yaml_update.setdefault("autopilot", {})[autopilot_keys[key]] = value

    # FIRST: write YAML to disk. Only update in-memory config on success.
    config_path = ASHLR_DIR / "ashlr.yaml"
    async with _config_write_lock:
        try:
            def _write_config():
                raw = DEFAULT_CONFIG.copy()
                if config_path.exists():
                    with open(config_path) as f:
                        raw = deep_merge(raw, yaml.safe_load(f) or {})
                raw = deep_merge(raw, yaml_update)
                tmp_path = config_path.with_suffix(".yaml.tmp")
                with open(tmp_path, "w") as f:
                    yaml.dump(raw, f, default_flow_style=False, sort_keys=False)
                tmp_path.rename(config_path)
            await asyncio.to_thread(_write_config)
            log.info(f"Config saved to disk: {', '.join(data.keys())}")
        except Exception as e:
            log.warning(f"Failed to save config to disk: {e}")
            try:
                config_path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
            except Exception as e2:
                log.debug(f"Failed to clean up temp config file: {e2}")
            # Do NOT update in-memory config — disk write failed
            return web.json_response({"error": f"Failed to save: {e}", "config": config.to_dict()}, status=500)

    # THEN: update in-memory config (disk write succeeded)
    for key in allowed_keys:
        if key in data and hasattr(config, key):
            setattr(config, key, data[key])

    # Update intelligence client config reference
    client = request.app.get("intelligence")
    if client:
        client.config = config

    # Recompile alert patterns if config changed
    if "alert_patterns" in data and isinstance(data["alert_patterns"], list):
        compiled_alerts = []
        for ap in data["alert_patterns"]:
            try:
                compiled_alerts.append((re.compile(ap["pattern"]), ap.get("severity", "warning"), ap.get("label", "Alert")))
            except (re.error, KeyError):
                pass
        request.app["_compiled_alert_patterns"] = compiled_alerts if compiled_alerts else None

    return web.json_response(config.to_dict())


# ── Project endpoints ──

async def list_projects(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    projects = await db.get_projects()
    # Enrich with agent counts and cost
    for proj in projects:
        agents = [a for a in list(manager.agents.values()) if a.project_id == proj["id"]]
        proj["agent_count"] = len(agents)
        proj["active_count"] = sum(1 for a in agents if a.status in ("working", "planning", "reading"))
        proj["total_cost"] = round(sum(a.estimated_cost_usd for a in agents), 4)
    return web.json_response(projects)


async def create_project(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = data.get("name")
    path = data.get("path")
    if not name or not isinstance(name, str) or not path or not isinstance(path, str):
        return web.json_response({"error": "name and path are required strings"}, status=400)

    resolved_path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved_path):
        return web.json_response({"error": f"Path is not a valid directory: {resolved_path}"}, status=400)
    home = str(Path.home())
    allowed_prefixes = [home, "/tmp", "/private/tmp"]
    if not any(resolved_path == p or resolved_path.startswith(p + os.sep) for p in allowed_prefixes):
        return web.json_response({"error": "Project path must be under home directory or /tmp"}, status=400)

    # Auto-detect git metadata
    git_remote_url = ""
    default_branch = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", resolved_path, "remote", "get-url", "origin",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0 and stdout:
            git_remote_url = stdout.decode().strip()
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", resolved_path, "symbolic-ref", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode == 0 and stdout:
            default_branch = stdout.decode().strip()
    except Exception:
        pass

    project = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "path": resolved_path,
        "description": str(data.get("description", "")),
        "git_remote_url": git_remote_url,
        "default_branch": default_branch,
        "default_backend": str(data.get("default_backend", "")),
        "default_model": str(data.get("default_model", "")),
        "default_role": str(data.get("default_role", "")),
        "tags": data.get("tags", []) if isinstance(data.get("tags"), list) else [],
        "favorite": bool(data.get("favorite", False)),
    }
    try:
        await db.save_project(project)
    except Exception as e:
        log.error("Failed to save project: %s", e)
        return web.json_response({"error": "Failed to save project"}, status=500)
    return web.json_response(project, status=201)


async def get_project_context(request: web.Request) -> web.Response:
    """GET /api/projects/{id}/context — project details + recent tasks + active agents + git info."""
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    project_id = request.match_info["id"]

    project = await db.get_project(project_id)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    # Active agents for this project
    agents = [a.to_dict() for a in list(manager.agents.values()) if a.project_id == project_id]

    # Aggregate stats
    total_cost = round(sum(a.get("estimated_cost_usd", 0) for a in agents), 4)
    files_touched = set()
    for a_obj in list(manager.agents.values()):
        if a_obj.project_id == project_id:
            files_touched |= a_obj._files_touched_set

    return web.json_response({
        "project": project,
        "recent_tasks": project.get("recent_tasks", []),
        "agents": agents,
        "stats": {
            "agent_count": len(agents),
            "active_count": sum(1 for a in agents if a.get("status") in ("working", "planning", "reading")),
            "total_cost": total_cost,
            "files_touched": len(files_touched),
        },
    })


async def delete_project(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    success = await db.delete_project(project_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Project not found"}, status=404)


async def update_project(request: web.Request) -> web.Response:
    """PUT /api/projects/{id} — update project fields (name, path, description, defaults, tags, favorite, etc.)."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    project_id = request.match_info["id"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict) or not data:
        return web.json_response({"error": "Request body must be a non-empty JSON object"}, status=400)

    # Validate path if provided
    if "path" in data:
        path = data["path"]
        if not isinstance(path, str) or not path:
            return web.json_response({"error": "path must be a non-empty string"}, status=400)
        resolved_path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(resolved_path):
            return web.json_response({"error": f"Path is not a valid directory: {resolved_path}"}, status=400)
        home = str(Path.home())
        if not (resolved_path.startswith(home) or resolved_path.startswith("/tmp") or resolved_path.startswith("/private/tmp")):
            return web.json_response({"error": "Project path must be under home directory or /tmp"}, status=400)
        data["path"] = resolved_path

    # Validate name if provided
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "name must be a non-empty string"}, status=400)
        data["name"] = name.strip()

    # Validate default_role if provided
    if "default_role" in data and data["default_role"]:
        if data["default_role"] not in BUILTIN_ROLES:
            return web.json_response({"error": f"Unknown role '{data['default_role']}'"}, status=400)

    # Validate tags if provided
    if "tags" in data:
        if not isinstance(data["tags"], list) or not all(isinstance(t, str) for t in data["tags"]):
            return web.json_response({"error": "tags must be a list of strings"}, status=400)

    # Validate favorite if provided
    if "favorite" in data:
        data["favorite"] = bool(data["favorite"])

    # Validate auto_approve_patterns if provided
    if "auto_approve_patterns" in data:
        patterns = data["auto_approve_patterns"]
        if not isinstance(patterns, list):
            return web.json_response({"error": "auto_approve_patterns must be a list"}, status=400)
        validated = []
        for ap in patterns:
            if not isinstance(ap, dict) or "pattern" not in ap:
                return web.json_response({"error": "Each auto_approve_pattern must be a dict with 'pattern' key"}, status=400)
            try:
                re.compile(ap["pattern"])
            except re.error as e:
                return web.json_response({"error": f"Invalid regex in auto_approve_patterns: {e}"}, status=400)
            if not ap["pattern"].strip():
                return web.json_response({"error": "Pattern cannot be empty"}, status=400)
            validated.append({"pattern": ap["pattern"], "response": str(ap.get("response", "yes"))[:200]})
        data["auto_approve_patterns"] = validated

    updated = await db.update_project(project_id, data)
    if not updated:
        return web.json_response({"error": "Project not found"}, status=404)

    # Broadcast update to all connected clients
    await hub.broadcast({"type": "project_updated", "project": updated})

    return web.json_response(updated)


# ── GitHub integration ──


async def _run_gh(args: list[str], cwd: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Run a gh CLI command. Returns (success, stdout_or_error)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return True, stdout.decode().strip()
        return False, stderr.decode().strip()
    except FileNotFoundError:
        return False, "gh CLI not found"
    except asyncio.TimeoutError:
        return False, "gh command timed out"
    except Exception as e:
        return False, redact_secrets(str(e))


async def github_status(request: web.Request) -> web.Response:
    """GET /api/github/status — check gh CLI availability and auth status."""
    ok, output = await _run_gh(["auth", "status", "--hostname", "github.com"])
    if ok:
        return web.json_response({"available": True, "authenticated": True, "detail": output})
    # Check if gh exists but not authed
    exists_ok, _ = await _run_gh(["--version"])
    if exists_ok:
        return web.json_response({"available": True, "authenticated": False, "detail": output})
    return web.json_response({"available": False, "authenticated": False, "detail": "gh CLI not installed"})


async def github_project_info(request: web.Request) -> web.Response:
    """GET /api/projects/{id}/github — fetch GitHub repo info (PRs, issues, branches)."""
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    project = await db.get_project(project_id)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    path = project.get("path", "")
    if not path or not os.path.isdir(os.path.expanduser(path)):
        return web.json_response({"error": "Project path not found on disk"}, status=400)
    cwd = os.path.expanduser(path)

    # Verify this is a GitHub repo
    ok, remote = await _run_gh(["repo", "view", "--json", "nameWithOwner,url,description,defaultBranchRef,isPrivate,stargazerCount,forkCount"], cwd=cwd)
    if not ok:
        return web.json_response({"error": "Not a GitHub repository or gh not authenticated", "detail": remote}, status=400)

    try:
        repo_info = json.loads(remote)
    except json.JSONDecodeError:
        return web.json_response({"error": "Failed to parse repo info"}, status=500)

    # Fetch open PRs
    pr_ok, pr_output = await _run_gh(["pr", "list", "--json", "number,title,state,author,createdAt,headRefName,url", "--limit", "10"], cwd=cwd)
    prs = json.loads(pr_output) if pr_ok and pr_output else []

    # Fetch open issues
    issue_ok, issue_output = await _run_gh(["issue", "list", "--json", "number,title,state,author,createdAt,labels,url", "--limit", "10"], cwd=cwd)
    issues = json.loads(issue_output) if issue_ok and issue_output else []

    # Fetch branches
    branch_ok, branch_output = await _run_gh(["api", "repos/{owner}/{repo}/branches", "--jq", ".[].name", "--paginate"], cwd=cwd, timeout=15.0)
    branches = branch_output.split("\n") if branch_ok and branch_output else []

    return web.json_response({
        "repo": repo_info,
        "pull_requests": prs,
        "issues": issues,
        "branches": [b for b in branches if b][:30],
    })


async def github_create_issue(request: web.Request) -> web.Response:
    """POST /api/projects/{id}/github/issues — create a GitHub issue."""
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    project = await db.get_project(project_id)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    title = data.get("title", "").strip()
    body = data.get("body", "").strip()
    labels = data.get("labels", [])
    if not title:
        return web.json_response({"error": "Title is required"}, status=400)

    cwd = os.path.expanduser(project.get("path", ""))
    args = ["issue", "create", "--title", title]
    if body:
        args += ["--body", body]
    for label in labels[:5]:
        args += ["--label", str(label)]

    ok, output = await _run_gh(args, cwd=cwd)
    if ok:
        return web.json_response({"url": output}, status=201)
    return web.json_response({"error": redact_secrets(output)}, status=400)


async def github_create_pr(request: web.Request) -> web.Response:
    """POST /api/projects/{id}/github/pulls — create a GitHub pull request."""
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    project = await db.get_project(project_id)
    if not project:
        return web.json_response({"error": "Project not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    title = data.get("title", "").strip()
    body = data.get("body", "").strip()
    head = data.get("head", "").strip()
    base = data.get("base", "").strip()
    if not title:
        return web.json_response({"error": "Title is required"}, status=400)

    cwd = os.path.expanduser(project.get("path", ""))
    args = ["pr", "create", "--title", title]
    if body:
        args += ["--body", body]
    if head:
        args += ["--head", head]
    if base:
        args += ["--base", base]

    ok, output = await _run_gh(args, cwd=cwd)
    if ok:
        return web.json_response({"url": output}, status=201)
    return web.json_response({"error": redact_secrets(output)}, status=400)


# ── Workflow endpoints ──

async def list_workflows(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflows = await db.get_workflows()
    return web.json_response(workflows)


def _validate_workflow_specs(agent_specs: list) -> str | None:
    """Validate workflow agent specs for structure, deps, and circular deps. Returns error string or None."""
    if not isinstance(agent_specs, list) or len(agent_specs) == 0:
        return "agents must be a non-empty list"

    valid_indices = set(range(len(agent_specs)))
    for i, spec in enumerate(agent_specs):
        if not isinstance(spec, dict):
            return f"agents[{i}] must be an object"
        if not spec.get("role"):
            return f"agents[{i}] missing required field 'role'"
        deps = spec.get("depends_on")
        if deps:
            if not isinstance(deps, list):
                return f"agents[{i}].depends_on must be a list"
            for dep in deps:
                if not isinstance(dep, int) or dep not in valid_indices:
                    return f"agents[{i}].depends_on contains invalid index {dep} (valid: 0-{len(agent_specs)-1})"
                if dep == i:
                    return f"agents[{i}].depends_on cannot reference itself"

    # Circular dependency check via topological sort (DFS)
    WHITE, GRAY, BLACK = 0, 1, 2
    colors = [WHITE] * len(agent_specs)

    def has_cycle(node: int) -> bool:
        colors[node] = GRAY
        for dep in (agent_specs[node].get("depends_on") or []):
            if isinstance(dep, int) and 0 <= dep < len(agent_specs):
                if colors[dep] == GRAY:
                    return True
                if colors[dep] == WHITE and has_cycle(dep):
                    return True
        colors[node] = BLACK
        return False

    for i in range(len(agent_specs)):
        if colors[i] == WHITE and has_cycle(i):
            return f"Circular dependency detected involving agents[{i}]"

    return None


async def create_workflow(request: web.Request) -> web.Response:
    if r := _check_feature(request, "workflows"):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    wf_name = data.get("name")
    if not wf_name or not isinstance(wf_name, str) or not data.get("agents"):
        return web.json_response({"error": "name (string) and agents are required"}, status=400)

    agent_specs = data["agents"]
    error = _validate_workflow_specs(agent_specs)
    if error:
        return web.json_response({"error": error}, status=400)

    workflow = {
        "id": uuid.uuid4().hex[:8],
        "name": wf_name,
        "description": str(data.get("description", "")),
        "agents_json": agent_specs,
    }
    try:
        await db.save_workflow(workflow)
    except Exception as e:
        log.error("Failed to save workflow: %s", e)
        return web.json_response({"error": "Failed to save workflow"}, status=500)
    workflow["agents"] = data["agents"]
    return web.json_response(workflow, status=201)


async def run_workflow(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    workflow_id = request.match_info["id"]

    workflow = await db.get_workflow(workflow_id)
    if not workflow:
        return web.json_response({"error": "Workflow not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    working_dir = body.get("working_dir")

    # Feature gate: workflows require Pro
    if r := _check_feature(request, "workflows"):
        return r

    # Capacity pre-check
    config: Config = request.app["config"]
    max_agents = _effective_max_agents(request.app)
    agents_needed = len(workflow.get("agents", []))
    current_count = len(manager.agents)
    if current_count + agents_needed > max_agents:
        return web.json_response({
            "error": f"Not enough capacity: need {agents_needed} agents, "
                     f"but only {max_agents - current_count} slots available "
                     f"({current_count}/{max_agents} in use)",
        }, status=503)

    agent_specs = workflow.get("agents", [])
    has_deps = any(spec.get("depends_on") for spec in agent_specs)

    if has_deps:
        # Check for circular dependencies before starting
        cycles = WorkflowRun.detect_circular_deps(agent_specs)
        if cycles:
            cycle_desc = "; ".join(f"[{' -> '.join(str(c) for c in cycle)}]" for cycle in cycles[:3])
            return web.json_response({
                "error": f"Circular dependency detected in workflow: {cycle_desc}",
                "cycles": cycles[:3],
            }, status=400)

        # DAG pipeline mode — create WorkflowRun and start with root agents
        run_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        # Per-stage timeout from request or workflow config (default 30 min)
        stage_timeout = body.get("stage_timeout_sec", workflow.get("stage_timeout_sec", 1800.0))
        try:
            stage_timeout = max(60.0, min(float(stage_timeout), 7200.0))  # Clamp 1min-2hr
        except (TypeError, ValueError):
            stage_timeout = 1800.0

        wf_run = WorkflowRun(
            id=run_id,
            workflow_id=workflow_id,
            workflow_name=workflow["name"],
            agent_specs=agent_specs,
            pending_indices=set(range(len(agent_specs))),
            working_dir=working_dir or config.default_working_dir,
            created_at=now,
            stage_timeout_sec=stage_timeout,
        )
        manager.workflow_runs[run_id] = wf_run

        # Spawn agents with no dependencies (root nodes)
        await manager.resolve_workflow_deps(wf_run, hub)

        await hub.broadcast_event(
            "workflow_started",
            f"Pipeline '{workflow['name']}' started (run {run_id}, {agents_needed} agents)",
            metadata={"workflow_run_id": run_id, "has_deps": True},
        )

        return web.json_response({
            "workflow_run_id": run_id,
            "workflow": workflow["name"],
            "pipeline": True,
            "agent_map": {str(k): v for k, v in wf_run.agent_map.items()},
            "pending": list(wf_run.pending_indices),
        })
    else:
        # Legacy parallel mode — spawn all at once
        agent_ids = []
        failed = []
        for agent_def in agent_specs:
            try:
                agent = await manager.spawn(
                    role=agent_def.get("role", "general"),
                    name=agent_def.get("name"),
                    working_dir=agent_def.get("working_dir") or working_dir,
                    task=agent_def.get("task", ""),
                    backend=agent_def.get("backend", config.default_backend),
                    model=agent_def.get("model"),
                    tools=agent_def.get("tools"),
                )
                agent_ids.append(agent.id)
            except ValueError as e:
                log.warning(f"Workflow spawn failed: {e}")
                failed.append({"role": agent_def.get("role", "general"), "error": redact_secrets(str(e))})

        # Link related agents
        for aid in agent_ids:
            a = manager.agents.get(aid)
            if a:
                a.related_agents = [x for x in agent_ids if x != aid]
                await hub.broadcast({"type": "agent_update", "agent": a.to_dict()})

        await hub.broadcast_event(
            "workflow_started",
            f"Workflow '{workflow['name']}' started ({len(agent_ids)} agents)",
        )

        result: dict[str, Any] = {"agent_ids": agent_ids, "workflow": workflow["name"]}
        if failed:
            result["spawned"] = agent_ids
            result["failed"] = failed

        return web.json_response(result)


async def list_workflow_runs(request: web.Request) -> web.Response:
    """GET /api/workflow-runs — list active and recent workflow runs."""
    manager: AgentManager = request.app["agent_manager"]
    runs = [wr.to_dict() for wr in manager.workflow_runs.values()]
    return web.json_response(runs)


async def get_workflow_run(request: web.Request) -> web.Response:
    """GET /api/workflow-runs/{id} — get details of a workflow run."""
    manager: AgentManager = request.app["agent_manager"]
    run_id = request.match_info["id"]
    wf_run = manager.workflow_runs.get(run_id)
    if not wf_run:
        return web.json_response({"error": "Workflow run not found"}, status=404)
    return web.json_response(wf_run.to_dict())


async def cancel_workflow_run(request: web.Request) -> web.Response:
    """POST /api/workflow-runs/{id}/cancel — cancel a running workflow pipeline."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    run_id = request.match_info["id"]
    wf_run = manager.workflow_runs.get(run_id)
    if not wf_run:
        return web.json_response({"error": "Workflow run not found"}, status=404)
    if wf_run.status != "running":
        return web.json_response({"error": f"Workflow is already {wf_run.status}"}, status=400)

    wf_run.status = "cancelled"
    wf_run.completed_at = datetime.now(timezone.utc).isoformat()
    wf_run.pending_indices.clear()

    # Kill running agents
    for aid in list(wf_run.running_ids):
        await manager.kill(aid)
        await hub.broadcast({"type": "agent_removed", "agent_id": aid})

    wf_run.running_ids.clear()
    await hub.broadcast_event(
        "workflow_cancelled",
        f"Pipeline '{wf_run.workflow_name}' cancelled",
        metadata={"workflow_run_id": run_id},
    )
    return web.json_response({"status": "cancelled", "workflow_run_id": run_id})


async def delete_workflow(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    workflow_id = request.match_info["id"]
    if workflow_id.startswith("builtin-"):
        return web.json_response({"error": "Cannot delete built-in workflows"}, status=400)
    success = await db.delete_workflow(workflow_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Workflow not found"}, status=404)


async def update_workflow(request: web.Request) -> web.Response:
    """PUT /api/workflows/{id} — update an existing workflow."""
    if r := _check_feature(request, "workflows"):
        return r
    db: Database = request.app["db"]
    workflow_id = request.match_info["id"]

    if workflow_id.startswith("builtin-"):
        return web.json_response({"error": "Cannot edit built-in workflows"}, status=400)

    existing = await db.get_workflow(workflow_id)
    if not existing:
        return web.json_response({"error": "Workflow not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    name = data.get("name", existing["name"])
    description = data.get("description", existing.get("description", ""))
    agents = data.get("agents", existing.get("agents", []))

    if not name or not agents:
        return web.json_response({"error": "name and agents are required"}, status=400)

    # Full validation including circular dep check
    error = _validate_workflow_specs(agents)
    if error:
        return web.json_response({"error": error}, status=400)

    workflow = {
        "id": workflow_id,
        "name": name,
        "description": description,
        "agents_json": agents,
        "created_at": existing.get("created_at", datetime.now(timezone.utc).isoformat()),
    }
    await db.save_workflow(workflow)

    return web.json_response({"id": workflow_id, "name": name, "description": description, "agents": agents})


# ── Backend endpoints ──

async def list_backends(request: web.Request) -> web.Response:
    """GET /api/backends — list available backends with rich capability info."""
    manager: AgentManager = request.app["agent_manager"]
    result = {}
    for name, bc in manager.backend_configs.items():
        d = bc.to_dict()
        d["name"] = name
        result[name] = d
    return web.json_response(result)


# ── Agent Messaging endpoints ──

async def send_agent_message(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/message — send message from one agent to another."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    manager: AgentManager = request.app["agent_manager"]
    from_agent_id = request.match_info["id"]

    from_agent = manager.agents.get(from_agent_id)
    if not from_agent:
        return web.json_response({"error": "Sender agent not found"}, status=404)

    if r := _check_agent_ownership(request, from_agent):
        return r

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_agent_id = data.get("to_agent_id")
    content = data.get("content", "")
    if not to_agent_id or not content:
        return web.json_response({"error": "to_agent_id and content required"}, status=400)
    if len(content) > 50000:
        return web.json_response({"error": "content too long (max 50,000 chars)"}, status=400)

    to_agent = manager.agents.get(to_agent_id)
    if not to_agent:
        return web.json_response({"error": "Target agent not found"}, status=404)

    msg = {
        "id": uuid.uuid4().hex[:8],
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.save_message(msg)
    to_agent.unread_messages += 1

    # Send to tmux session
    sanitized = content.strip()[:500]
    sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)  # Strip control chars except newline/tab
    await manager.send_message(to_agent_id, f"[Message from {from_agent.name}]: {sanitized}")

    await hub.broadcast({"type": "agent_message", "message": msg})
    await hub.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})

    return web.json_response(msg, status=201)


async def get_agent_messages(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/messages — get messages for an agent."""
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)

    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    messages = await db.get_messages_for_agent(agent_id, limit, offset)
    total = await db.get_message_count_for_agent(agent_id)

    # Mark as read
    read_count = await db.mark_messages_read(agent_id)
    if read_count > 0:
        agent.unread_messages = 0
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

    return web.json_response({
        "data": messages,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


# ── Agent Handoff endpoint ──

async def handoff_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{from_id}/handoff — structured handoff from one agent to another."""
    db: Database = request.app["db"]
    hub: WebSocketHub = request.app["ws_hub"]
    manager: AgentManager = request.app["agent_manager"]
    from_id = request.match_info["id"]

    from_agent = manager.agents.get(from_id)
    if not from_agent:
        return web.json_response({"error": "Source agent not found"}, status=404)

    if err := _check_agent_ownership(request, from_agent):
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    to_id = data.get("to_agent_id")
    if not to_id:
        return web.json_response({"error": "to_agent_id is required"}, status=400)

    to_agent = manager.agents.get(to_id)
    if not to_agent:
        return web.json_response({"error": "Target agent not found"}, status=404)

    key_findings = str(data.get("key_findings", ""))[:5000]
    files_modified = data.get("files_modified", [])
    if not isinstance(files_modified, list):
        return web.json_response({"error": "files_modified must be a list"}, status=400)
    files_modified = [str(f)[:500] for f in files_modified[:50]]

    # Build handoff block
    from_role = BUILTIN_ROLES.get(from_agent.role, BUILTIN_ROLES["general"])
    handoff_block = (
        f"[HANDOFF from {from_agent.name} ({from_role.name})]:\n"
        f"Summary: {from_agent.summary}\n"
    )
    if key_findings:
        handoff_block += f"Key findings: {key_findings}\n"
    if files_modified:
        handoff_block += f"Files modified: {', '.join(files_modified)}\n"
    # Add last 20 lines of output as context
    recent = list(from_agent.output_lines)[-20:]
    if recent:
        handoff_block += f"Recent output:\n" + "\n".join(recent)

    # Save as structured message
    msg = {
        "id": uuid.uuid4().hex[:8],
        "from_agent_id": from_id,
        "to_agent_id": to_id,
        "content": handoff_block,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.save_message(msg)
    to_agent.unread_messages += 1

    # Send to target agent's tmux session
    await manager.send_message(to_id, handoff_block[:2000])

    # Auto-write handoff data to scratchpad if agent has a project
    if from_agent.project_id:
        if key_findings:
            await db.upsert_scratchpad(from_agent.project_id, f"handoff_{from_agent.name}_findings", key_findings, from_agent.name)
        if files_modified:
            await db.upsert_scratchpad(from_agent.project_id, f"handoff_{from_agent.name}_files", ", ".join(files_modified), from_agent.name)

    await hub.broadcast({"type": "agent_message", "message": msg})
    await hub.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})
    await hub.broadcast_event(
        "agent_handoff",
        f"Handoff: {from_agent.name} → {to_agent.name}",
        from_id, from_agent.name,
        {"to_agent_id": to_id, "to_agent_name": to_agent.name},
    )

    return web.json_response({"status": "handed_off", "message_id": msg["id"]})


# ── Intelligence Endpoints ──

async def get_agent_activity(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/activity — structured tool/file/git/test data."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    activity = _intelligence_parser.get_activity_summary(agent)
    return web.json_response(activity)


async def get_agent_tool_invocations(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/tool-invocations — tool invocation history."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    return web.json_response({
        "agent_id": agent_id,
        "invocations": [t.to_dict() for t in list(agent._tool_invocations)[-limit:]],
        "total": len(agent._tool_invocations),
    })


async def get_agent_file_operations(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/file-operations — file operation history."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100
    return web.json_response({
        "agent_id": agent_id,
        "operations": [f.to_dict() for f in list(agent._file_operations)[-limit:]],
        "total": len(agent._file_operations),
    })


async def search_agent_output(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/output/search?q=pattern&regex=false&context=2&limit=100 — search agent output."""
    if r := _check_rate(request, cost=2):
        return r
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    query = request.query.get("q", "").strip()
    if not query or len(query) > 500:
        return web.json_response({"error": "Query 'q' required (max 500 chars)"}, status=400)

    use_regex = request.query.get("regex", "false").lower() in ("true", "1")
    try:
        context_lines = max(0, min(int(request.query.get("context", "2")), 10))
    except ValueError:
        context_lines = 2
    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 500))
    except ValueError:
        limit = 100

    # Compile pattern
    try:
        if use_regex:
            pattern = re.compile(query, re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
    except re.error as e:
        return web.json_response({"error": f"Invalid regex: {e}"}, status=400)

    lines = list(agent.output_lines)
    matches = []
    for i, line in enumerate(lines):
        stripped = _strip_ansi(line) if callable(_strip_ansi) else line
        if pattern.search(stripped):
            # Gather context
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            ctx = [_strip_ansi(lines[j]) if callable(_strip_ansi) else lines[j] for j in range(start, end)]
            matches.append({
                "line_index": i + agent._archived_lines,
                "line": stripped[:500],
                "context": ctx,
            })
            if len(matches) >= limit:
                break

    return web.json_response({
        "agent_id": agent_id,
        "query": query,
        "regex": use_regex,
        "matches": matches,
        "total_matches": len(matches),
        "total_lines": len(lines) + agent._archived_lines,
    })


async def get_agent_snapshots(request: web.Request) -> web.Response:
    """GET /api/agents/{id}/snapshots — output snapshots at key lifecycle points."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    return web.json_response({
        "agent_id": agent_id,
        "snapshots": [s.to_dict() for s in agent._snapshots],
        "total": len(agent._snapshots),
    })


async def create_agent_snapshot(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/snapshots — create a manual snapshot."""
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if err := _check_agent_ownership(request, agent):
        return err
    snap = agent.create_snapshot(trigger="manual")
    return web.json_response(snap.to_dict(), status=201)


async def get_intelligence_insights(request: web.Request) -> web.Response:
    """GET /api/intelligence/insights — current cross-agent insights."""
    if r := _check_feature(request, "intelligence"):
        return r
    insights: list[AgentInsight] = request.app.get("intelligence_insights", [])
    return web.json_response({
        "insights": [i.to_dict() for i in insights if not i.acknowledged],
        "total": len(insights),
    })


async def acknowledge_insight(request: web.Request) -> web.Response:
    """POST /api/intelligence/insights/{id}/ack — acknowledge an insight."""
    insights: list[AgentInsight] = request.app.get("intelligence_insights", [])
    insight_id = request.match_info["id"]
    for insight in insights:
        if insight.id == insight_id:
            insight.acknowledged = True
            return web.json_response({"status": "acknowledged"})
    return web.json_response({"error": "Insight not found"}, status=404)


async def intelligence_command(request: web.Request) -> web.Response:
    """POST /api/intelligence/command — parse natural language command via Claude API."""
    if r := _check_feature(request, "intelligence"):
        return r
    if r := _check_rate(request, cost=5, rate=0.5, burst=5):
        return r
    client: IntelligenceClient | None = request.app.get("intelligence")
    manager: AgentManager = request.app["agent_manager"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    transcript = data.get("transcript", "").strip()
    if not transcript:
        return web.json_response({"error": "Empty transcript"}, status=400)

    # If intelligence client is available, use it for NLU
    if client and client.available:
        try:
            intent = await client.parse_command(
                transcript,
                list(manager.agents.values()),
                {"total_agents": len(manager.agents)},
            )
            return web.json_response({
                "intent": intent.to_dict(),
                "source": "xai",
            })
        except Exception as e:
            log.warning(f"Intelligence command parse failed: {e}")

    # Fallback to keyword matching
    intent = _keyword_parse_command(transcript, list(manager.agents.values()))
    return web.json_response({
        "intent": intent.to_dict(),
        "source": "keyword",
    })


def _keyword_parse_command(transcript: str, agents: list) -> ParsedIntent:
    """Fallback keyword-based command parsing."""
    t = transcript.lower().strip()

    # Spawn patterns
    if any(w in t for w in ("spawn", "start", "create", "launch", "new agent")):
        role = "general"
        for r in BUILTIN_ROLES:
            if r in t:
                role = r
                break
        return ParsedIntent(action="spawn", parameters={"role": role}, confidence=0.6)

    # Kill patterns
    if any(w in t for w in ("kill", "stop", "terminate", "remove")):
        targets = _resolve_agent_refs(t, agents)
        if "all" in t:
            targets = [a.id for a in agents]
        return ParsedIntent(action="kill", targets=targets, confidence=0.6)

    # Pause/resume patterns
    if any(w in t for w in ("pause", "freeze", "hold")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="pause", targets=targets, confidence=0.6)
    if any(w in t for w in ("resume", "unpause", "continue")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="resume", targets=targets, confidence=0.6)

    # Status query
    if any(w in t for w in ("status", "what", "how", "doing")):
        targets = _resolve_agent_refs(t, agents)
        return ParsedIntent(action="status", targets=targets, confidence=0.5)

    # Send message
    if any(w in t for w in ("tell", "send", "approve", "reject", "yes", "no")):
        targets = _resolve_agent_refs(t, agents)
        message = transcript  # Use full transcript as the message
        if "approve" in t:
            message = "yes, proceed"
        elif "reject" in t:
            message = "no, stop"
        return ParsedIntent(action="send", targets=targets, message=message, confidence=0.5)

    return ParsedIntent(action="unknown", message=transcript, confidence=0.2)


def _resolve_agent_refs(text: str, agents: list) -> list[str]:
    """Resolve agent references in natural language text."""
    resolved = []
    for agent in agents:
        if agent.name.lower() in text or agent.id.lower() in text:
            resolved.append(agent.id)
    # Try numeric references ("agent 1", "agent 2")
    import re as _re
    for m in _re.finditer(r'agent\s*(\d+)', text):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(agents):
            resolved.append(agents[idx].id)
    return resolved


# ── LLM Summary endpoint ──

async def generate_summary(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/summarize — manually trigger LLM summary."""
    if r := _check_rate(request, cost=3, rate=0.5, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    client: IntelligenceClient | None = request.app.get("intelligence")
    agent_id = request.match_info["id"]
    agent = manager.agents.get(agent_id)

    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)
    if not client or not client.available:
        return web.json_response({"error": "LLM not configured"}, status=503)

    summary = await client.summarize(
        list(agent.output_lines), agent.task, agent.role, agent.status
    )
    if summary:
        agent.summary = summary
        agent._llm_summary = summary
        hub: WebSocketHub = request.app["ws_hub"]
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"summary": summary})
    return web.json_response({"error": "LLM summary generation failed", "fallback": agent.summary}, status=503)


# ── Cost tracking endpoint ──

async def get_costs(request: web.Request) -> web.Response:
    """GET /api/costs — aggregate cost estimates across active and historical agents."""
    manager: AgentManager = request.app["agent_manager"]
    db: Database = request.app["db"]

    # Active agents
    active_cost = 0.0
    active_tokens_in = 0
    active_tokens_out = 0
    for agent in list(manager.agents.values()):
        active_cost += agent.estimated_cost_usd
        active_tokens_in += agent.tokens_input
        active_tokens_out += agent.tokens_output

    # Historical (from DB)
    history = await db.get_agent_history(limit=200)
    hist_cost = sum(h.get("estimated_cost_usd", 0) or 0 for h in history)
    hist_tokens_in = sum(h.get("tokens_input", 0) or 0 for h in history)
    hist_tokens_out = sum(h.get("tokens_output", 0) or 0 for h in history)

    # Per-project cost breakdown
    project_costs: dict[str, float] = {}
    for agent in list(manager.agents.values()):
        pid = getattr(agent, 'project_id', '') or 'unassigned'
        project_costs[pid] = project_costs.get(pid, 0) + agent.estimated_cost_usd

    # Budget info
    config: Config = request.app["config"]
    budget = config.cost_budget_usd
    total = active_cost + hist_cost
    budget_info = None
    if budget > 0:
        pct = total / budget if budget > 0 else 0
        # Estimate time to budget exhaustion from active burn rate
        burn_rates = [a._cost_burn_rate() for a in list(manager.agents.values()) if a.status in ("working", "planning", "reading")]
        total_burn_per_min = sum(br.get("rate_per_min", 0) for br in burn_rates if br)
        mins_remaining = (budget - total) / total_burn_per_min if total_burn_per_min > 0 and total < budget else None
        budget_info = {
            "budget_usd": budget,
            "used_pct": round(pct * 100, 1),
            "remaining_usd": round(max(0, budget - total), 4),
            "burn_rate_per_min": round(total_burn_per_min, 6),
            "minutes_remaining": round(mins_remaining, 1) if mins_remaining is not None else None,
        }

    return web.json_response({
        "active": {
            "cost_usd": round(active_cost, 4),
            "tokens_input": active_tokens_in,
            "tokens_output": active_tokens_out,
            "agent_count": len(manager.agents),
        },
        "historical": {
            "cost_usd": round(hist_cost, 4),
            "tokens_input": hist_tokens_in,
            "tokens_output": hist_tokens_out,
        },
        "total_cost_usd": round(total, 4),
        "by_project": {k: round(v, 4) for k, v in project_costs.items()},
        "budget": budget_info,
    })


# ── Agent Preset endpoints ──

async def list_presets(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    presets = await db.get_presets()
    return web.json_response(presets)


async def create_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data.get("name"):
        return web.json_response({"error": "name is required"}, status=400)

    role = data.get("role", "general")
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Invalid role '{role}'. Valid roles: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)
    backend = data.get("backend", "claude-code")
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if backend not in valid_backends:
        return web.json_response({"error": f"Invalid backend '{backend}'. Valid backends: {', '.join(sorted(valid_backends))}"}, status=400)

    preset = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"][:100],
        "role": role,
        "backend": backend,
        "task": data.get("task", "")[:10000],
        "system_prompt": data.get("system_prompt", "")[:5000],
        "model": data.get("model", ""),
        "tools_allowed": data.get("tools_allowed", ""),
        "working_dir": data.get("working_dir", ""),
    }
    try:
        await db.save_preset(preset)
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": f"Preset '{preset['name']}' already exists"}, status=409)
        raise
    return web.json_response(preset, status=201)


async def update_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    preset_id = request.match_info["id"]
    existing = await db.get_preset(preset_id)
    if not existing:
        return web.json_response({"error": "Preset not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    updated = {**existing}
    for key in ("name", "role", "backend", "task", "system_prompt", "model", "tools_allowed", "working_dir"):
        if key in data:
            updated[key] = data[key]
    updated["name"] = updated["name"][:100]
    updated["task"] = updated["task"][:10000]

    if updated.get("role") and updated["role"] not in BUILTIN_ROLES:
        return web.json_response({"error": f"Invalid role '{updated['role']}'. Valid roles: {', '.join(BUILTIN_ROLES.keys())}"}, status=400)
    valid_backends = set(KNOWN_BACKENDS.keys()) | set(request.app["config"].backends.keys())
    if updated.get("backend") and updated["backend"] not in valid_backends:
        return web.json_response({"error": f"Invalid backend '{updated['backend']}'. Valid backends: {', '.join(sorted(valid_backends))}"}, status=400)

    try:
        await db.save_preset(updated)
    except Exception as e:
        if "UNIQUE" in str(e):
            return web.json_response({"error": f"Preset name '{updated['name']}' already exists"}, status=409)
        raise
    return web.json_response(updated)


async def delete_preset(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    preset_id = request.match_info["id"]
    success = await db.delete_preset(preset_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Preset not found"}, status=404)


# ── Fleet Templates ──


def _substitute_task_vars(task: str, project: dict | None = None, extra: dict | None = None) -> str:
    """Replace template variables in task strings."""
    now = datetime.now(timezone.utc)
    replacements = {
        "{date}": now.strftime("%Y-%m-%d"),
        "{time}": now.strftime("%H:%M"),
    }
    if project:
        replacements["{project_name}"] = project.get("name", "")
        replacements["{branch}"] = project.get("default_branch", "") or project.get("git_branch", "")
        # Strip control characters from git remote URL to prevent injection
        git_remote = re.sub(r'[\r\n\x00-\x1f\x7f]', '', project.get("git_remote_url", ""))
        replacements["{git_remote}"] = git_remote
    if extra:
        for k, v in extra.items():
            replacements[f"{{{k}}}"] = str(v)
    for placeholder, value in replacements.items():
        task = task.replace(placeholder, value)
    return task


async def list_fleet_templates(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    project_id = request.query.get("project_id")
    templates = await db.get_fleet_templates(project_id)
    return web.json_response(templates)


async def create_fleet_template(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data.get("name"):
        return web.json_response({"error": "name is required"}, status=400)
    agents = data.get("agents", [])
    if not isinstance(agents, list) or len(agents) == 0:
        return web.json_response({"error": "agents must be a non-empty list"}, status=400)
    if len(agents) > 20:
        return web.json_response({"error": "Maximum 20 agents per template"}, status=400)

    # Validate and sanitize each agent spec
    validated_agents = []
    for i, spec in enumerate(agents[:20]):
        if not isinstance(spec, dict):
            return web.json_response({"error": f"Agent spec #{i} must be a dict"}, status=400)
        if not spec.get("task"):
            return web.json_response({"error": f"Agent spec #{i} must have a 'task' field"}, status=400)
        validated_agents.append({
            "role": str(spec.get("role", "general"))[:50],
            "task": str(spec["task"])[:10000],
            "backend": str(spec.get("backend", ""))[:50],
            "model": str(spec.get("model", ""))[:50],
            "name": str(spec.get("name", ""))[:100],
            "working_dir": str(spec.get("working_dir", ""))[:500],
            "tools": str(spec.get("tools", ""))[:2000],
            "system_prompt": str(spec.get("system_prompt", ""))[:5000],
        })

    template = {
        "id": uuid.uuid4().hex[:8],
        "name": data["name"][:100],
        "description": data.get("description", "")[:500],
        "project_id": data.get("project_id", ""),
        "agents": validated_agents,
    }
    await db.save_fleet_template(template)
    return web.json_response(template, status=201)


async def get_fleet_template(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    template_id = request.match_info["id"]
    template = await db.get_fleet_template(template_id)
    if not template:
        return web.json_response({"error": "Template not found"}, status=404)
    return web.json_response(template)


async def update_fleet_template(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    template_id = request.match_info["id"]
    existing = await db.get_fleet_template(template_id)
    if not existing:
        return web.json_response({"error": "Template not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    for key in ("name", "description", "project_id", "agents"):
        if key in data:
            existing[key] = data[key]
    if existing.get("name"):
        existing["name"] = existing["name"][:100]
    if existing.get("description"):
        existing["description"] = existing["description"][:500]
    if isinstance(existing.get("agents"), list) and len(existing["agents"]) > 20:
        existing["agents"] = existing["agents"][:20]

    await db.save_fleet_template(existing)
    return web.json_response(existing)


async def delete_fleet_template(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    template_id = request.match_info["id"]
    success = await db.delete_fleet_template(template_id)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Template not found"}, status=404)


async def deploy_fleet_template(request: web.Request) -> web.Response:
    """Deploy a fleet template — spawn all agents in sequence."""
    if r := _check_rate(request, cost=5):
        return r
    if r := _check_feature(request, "fleet_presets"):
        return r
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    config: Config = request.app["config"]

    template_id = request.match_info["id"]
    template = await db.get_fleet_template(template_id)
    if not template:
        return web.json_response({"error": "Template not found"}, status=404)

    try:
        data = await request.json()
    except (json.JSONDecodeError, Exception):
        data = {}

    project_id = data.get("project_id", template.get("project_id", ""))
    project = None
    project_dict = None
    if project_id:
        project = await db.get_project(project_id)
        if project:
            project_dict = project.to_dict() if hasattr(project, "to_dict") else (dict(project) if isinstance(project, dict) else None)

    agents_spec = template.get("agents", [])
    if not agents_spec:
        return web.json_response({"error": "Template has no agents"}, status=400)

    # Check capacity
    max_agents = _effective_max_agents(request.app)
    current = len(manager.agents)
    if current + len(agents_spec) > max_agents:
        return web.json_response({
            "error": f"Would exceed max agents ({current} + {len(agents_spec)} > {max_agents})"
        }, status=409)

    spawned = []
    errors = []
    for spec in agents_spec:
        try:
            task_text = spec.get("task", "")
            task_text = _substitute_task_vars(task_text, project_dict, data.get("vars"))

            working_dir = spec.get("working_dir", "")
            if not working_dir and project_dict:
                working_dir = project_dict.get("path", "")
            if not working_dir:
                working_dir = config.default_working_dir

            agent = await manager.spawn(
                role=spec.get("role", config.default_role),
                task=task_text,
                name=spec.get("name", ""),
                working_dir=working_dir,
                backend=spec.get("backend", config.default_backend),
                model=spec.get("model", ""),
                tools=spec.get("tools") or None,
                system_prompt_extra=spec.get("system_prompt", "") or None,
            )
            if agent:
                agent.project_id = project_id
                spawned.append(agent.to_dict())
                await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        except Exception as e:
            errors.append({"spec": spec.get("role", "?"), "error": redact_secrets(str(e))})

    return web.json_response({
        "template": template["name"],
        "spawned": len(spawned),
        "errors": errors,
        "agents": spawned,
    }, status=201 if spawned else 400)


# ── Webhook endpoints ──

# SSRF protection: block private/reserved IP ranges
import ipaddress as _ipaddress

_BLOCKED_NETS = [
    _ipaddress.ip_network("127.0.0.0/8"),
    _ipaddress.ip_network("10.0.0.0/8"),
    _ipaddress.ip_network("172.16.0.0/12"),
    _ipaddress.ip_network("192.168.0.0/16"),
    _ipaddress.ip_network("169.254.0.0/16"),
    _ipaddress.ip_network("::1/128"),
    _ipaddress.ip_network("fc00::/7"),
    _ipaddress.ip_network("fe80::/10"),
]


def _validate_webhook_url(url: str) -> str | None:
    """Validate webhook URL. Returns error string or None if valid."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"
    if parsed.scheme not in ("https", "http"):
        return "URL must use https:// or http://"
    if not parsed.hostname:
        return "URL must have a hostname"
    hostname = parsed.hostname.lower()
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "Localhost URLs not allowed"
    # Resolve hostname and check against blocked ranges
    try:
        import socket
        addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in addrs:
            ip = _ipaddress.ip_address(sockaddr[0])
            for net in _BLOCKED_NETS:
                if ip in net:
                    return f"Private/reserved IP address not allowed: {ip}"
    except (socket.gaierror, ValueError):
        pass  # DNS resolution failed — allow (URL might be external)
    return None


async def list_webhooks(request: web.Request) -> web.Response:
    """GET /api/webhooks — list all webhooks."""
    db: Database = request.app["db"]
    webhooks = await db.get_webhooks()
    # Strip secrets from response
    for w in webhooks:
        if w.get("secret"):
            w["secret"] = "***"
    return web.json_response(webhooks)


async def create_webhook(request: web.Request) -> web.Response:
    """POST /api/webhooks — create a webhook."""
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    url = data.get("url", "").strip()
    if not url:
        return web.json_response({"error": "URL is required"}, status=400)

    url_err = _validate_webhook_url(url)
    if url_err:
        return web.json_response({"error": url_err}, status=400)

    events = data.get("events", [])
    if not isinstance(events, list):
        return web.json_response({"error": "events must be a list"}, status=400)

    # Generate HMAC secret
    import secrets as _secrets
    webhook_id = uuid.uuid4().hex[:8]
    secret = _secrets.token_hex(32)

    webhook = {
        "id": webhook_id,
        "url": url,
        "name": data.get("name", "").strip()[:100] or url[:60],
        "events": events[:20],  # Cap at 20 event types
        "secret": secret,
        "active": True,
    }
    await db.save_webhook(webhook)
    request.app["ws_hub"].invalidate_webhook_cache()
    return web.json_response(webhook, status=201)


async def update_webhook(request: web.Request) -> web.Response:
    """PUT /api/webhooks/{id} — update a webhook."""
    db: Database = request.app["db"]
    webhook_id = request.match_info["id"]
    existing = await db.get_webhook(webhook_id)
    if not existing:
        return web.json_response({"error": "Webhook not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if "url" in data:
        url = data["url"].strip()
        url_err = _validate_webhook_url(url)
        if url_err:
            return web.json_response({"error": url_err}, status=400)
        existing["url"] = url

    if "name" in data:
        existing["name"] = data["name"].strip()[:100]
    if "events" in data and isinstance(data["events"], list):
        existing["events"] = data["events"][:20]
    if "active" in data:
        existing["active"] = bool(data["active"])

    await db.save_webhook(existing)
    request.app["ws_hub"].invalidate_webhook_cache()
    if existing.get("secret"):
        existing["secret"] = "***"
    return web.json_response(existing)


async def delete_webhook(request: web.Request) -> web.Response:
    """DELETE /api/webhooks/{id} — delete a webhook."""
    db: Database = request.app["db"]
    webhook_id = request.match_info["id"]
    deleted = await db.delete_webhook(webhook_id)
    if not deleted:
        return web.json_response({"error": "Webhook not found"}, status=404)
    request.app["ws_hub"].invalidate_webhook_cache()
    return web.json_response({"deleted": True})


async def test_webhook(request: web.Request) -> web.Response:
    """POST /api/webhooks/{id}/test — send a test payload to a webhook."""
    db: Database = request.app["db"]
    webhook_id = request.match_info["id"]
    webhook = await db.get_webhook(webhook_id)
    if not webhook:
        return web.json_response({"error": "Webhook not found"}, status=404)

    test_payload = {
        "event": "webhook_test",
        "message": "This is a test webhook delivery from Ashlr AO",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Deliver synchronously for test
    try:
        import aiohttp as _aiohttp
        import hmac as _hmac
        import hashlib as _hashlib
        body = json.dumps(test_payload)
        headers = {"Content-Type": "application/json", "User-Agent": "Ashlr-AO-Webhook/1.0"}
        if webhook.get("secret"):
            sig = _hmac.new(webhook["secret"].encode(), body.encode(), _hashlib.sha256).hexdigest()
            headers["X-Ashlr-Signature"] = f"sha256={sig}"
        timeout = _aiohttp.ClientTimeout(total=10, connect=5)
        async with _aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(webhook["url"], data=body, headers=headers) as resp:
                return web.json_response({
                    "success": resp.status < 400,
                    "status_code": resp.status,
                    "response": (await resp.text())[:500],
                })
    except Exception as e:
        return web.json_response({"success": False, "error": redact_secrets(str(e))})


async def get_webhook_deliveries(request: web.Request) -> web.Response:
    """GET /api/webhooks/{id}/deliveries — delivery history for a webhook."""
    db: Database = request.app["db"]
    webhook_id = request.match_info["id"]
    webhook = await db.get_webhook(webhook_id)
    if not webhook:
        return web.json_response({"error": "Webhook not found"}, status=404)
    deliveries = await db.get_webhook_deliveries(webhook_id, limit=50)
    return web.json_response(deliveries)


# ── Agent Clone endpoint ──

async def get_resumable_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions/resumable — list recently completed agents that can be resumed."""
    db: Database = request.app["db"]
    try:
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
    except ValueError:
        limit = 20
    sessions = await db.get_resumable_sessions(limit)
    return web.json_response({"sessions": sessions})


async def resume_from_history(request: web.Request) -> web.Response:
    """POST /api/sessions/{id}/resume — re-spawn an agent from its archived session.

    Optionally override task or append instructions via 'task' and 'continue_message' fields.
    """
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    session_id = request.match_info["id"]

    # Look up the archived session
    session = await db.get_agent_history_item(session_id)
    if not session:
        return web.json_response({"error": "Session not found"}, status=404)

    # Parse optional override fields
    try:
        data = await request.json()
    except Exception:
        data = {}

    task = data.get("task", session.get("task", ""))
    continue_msg = data.get("continue_message", "")
    if continue_msg:
        task = f"{task}\n\nContinuation: {continue_msg}" if task else continue_msg
    plan_mode = data.get("plan_mode", bool(session.get("plan_mode", 0)))

    # Verify working directory still exists
    working_dir = session.get("working_dir", os.getcwd())
    if not os.path.isdir(working_dir):
        return web.json_response({"error": f"Working directory no longer exists: {working_dir}"}, status=400)

    # Check agent limit (license-aware)
    config: Config = request.app["config"]
    effective_max = _effective_max_agents(request.app)
    if len(manager.agents) >= effective_max:
        return web.json_response({"error": f"Agent limit reached ({effective_max})"}, status=409)

    # Spawn a new agent with the archived session's config
    backend = session.get("backend", config.default_backend)
    model = data.get("model") or session.get("model") or None
    tools_raw = data.get("tools") or session.get("tools_allowed") or None
    tools = tools_raw if isinstance(tools_raw, list) else None

    try:
        new_agent = await manager.spawn(
            name=session.get("name", "resumed"),
            role=session.get("role", "general"),
            task=task,
            working_dir=working_dir,
            backend=backend,
            plan_mode=plan_mode,
            model=model,
            tools=tools,
            resume_session=session_id,
        )
        if session.get("project_id"):
            new_agent.project_id = session["project_id"]
    except Exception as e:
        return web.json_response({"error": f"Failed to resume: {e}"}, status=500)

    # Broadcast the new agent
    await hub.broadcast({
        "type": "agent_update",
        "agent": new_agent.to_dict(),
    })
    await hub.broadcast_event("agent_spawned", f"Resumed {new_agent.name} from session {session_id[:8]}", new_agent.id)

    return web.json_response({
        "agent": new_agent.to_dict(),
        "resumed_from": session_id,
    }, status=201)


async def export_fleet_state(request: web.Request) -> web.Response:
    """GET /api/fleet/export — export current fleet state as JSON for backup/restore."""
    manager: AgentManager = request.app["agent_manager"]
    config: Config = request.app["config"]
    db: Database = request.app["db"]

    agents_data = []
    for agent in list(manager.agents.values()):
        agents_data.append({
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "status": agent.status,
            "task": agent.task,
            "summary": agent.summary,
            "working_dir": agent.working_dir,
            "backend": agent.backend,
            "project_id": agent.project_id,
            "plan_mode": agent.plan_mode,
            "context_pct": agent.context_pct,
            "tokens_input": agent.tokens_input,
            "tokens_output": agent.tokens_output,
            "estimated_cost_usd": agent.estimated_cost_usd,
            "created_at": agent.created_at,
            "health_score": agent.health_score,
            "error_count": agent.error_count,
            "restart_count": agent.restart_count,
        })

    projects = await db.get_projects() if db else []

    export_data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agents": agents_data,
        "agents_count": len(agents_data),
        "projects": projects,
        "config_summary": {
            "max_agents": config.max_agents,
            "default_backend": config.default_backend,
            "fleet_cost_limit": config.cost_budget_usd,
            "intelligence_enabled": config.llm_enabled,
        },
    }
    return web.json_response(export_data)


async def clone_agent(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/clone — clone an agent's config into a new agent."""
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    source_id = request.match_info["id"]

    source = manager.agents.get(source_id)
    if not source:
        return web.json_response({"error": "Source agent not found"}, status=404)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        data = {}

    working_dir = data.get("working_dir", source.working_dir)
    name = data.get("name", f"{source.name}-clone")

    if len(manager.agents) >= _effective_max_agents(request.app):
        return web.json_response({"error": "Max agents reached"}, status=409)

    try:
        agent = await manager.spawn(
            role=source.role,
            name=name,
            working_dir=working_dir,
            task=source.task,
            backend=source.backend,
            model=source.model,
            tools=source.tools_allowed,
            system_prompt_extra=source.system_prompt,
        )
        if source.project_id:
            agent.project_id = source.project_id
        await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        await hub.broadcast_event(
            "agent_spawned",
            f"Cloned agent: {agent.name} (from {source.name})",
            agent.id, agent.name,
        )
        return web.json_response({"agent": agent.to_dict()}, status=201)
    except ValueError as e:
        return web.json_response({"error": redact_secrets(str(e))}, status=400)


# ── Task Queue endpoints ──

async def list_queue(request: web.Request) -> web.Response:
    """GET /api/queue — list all queued tasks."""
    manager: AgentManager = request.app["agent_manager"]
    return web.json_response([t.to_dict() for t in manager.task_queue])


async def add_to_queue(request: web.Request) -> web.Response:
    """POST /api/queue — add a task to the auto-spawn queue."""
    if r := _check_rate(request, cost=2, rate=1, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    role = data.get("role", "general")
    if role not in BUILTIN_ROLES:
        return web.json_response({"error": f"Unknown role '{role}'"}, status=400)
    backend = data.get("backend")
    if backend:
        config_temp: Config = request.app["config"]
        valid_backends = set(KNOWN_BACKENDS.keys()) | set(config_temp.backends.keys())
        if backend not in valid_backends:
            return web.json_response({"error": f"Unknown backend '{backend}'"}, status=400)
    name = data.get("name", f"{role}-{uuid.uuid4().hex[:4]}")
    task_desc = data.get("task", "")
    if not task_desc:
        return web.json_response({"error": "task is required"}, status=400)

    config: Config = request.app["config"]
    queued = QueuedTask(
        id=uuid.uuid4().hex[:8],
        role=role,
        name=name,
        task=task_desc,
        working_dir=data.get("working_dir", config.default_working_dir),
        backend=data.get("backend", config.default_backend),
        plan_mode=data.get("plan_mode", False),
        project_id=data.get("project_id"),
        priority=data.get("priority", 0),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    manager.task_queue.append(queued)
    manager.task_queue.sort(key=lambda t: -t.priority)

    await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
    return web.json_response(queued.to_dict(), status=201)


async def remove_from_queue(request: web.Request) -> web.Response:
    """DELETE /api/queue/{id} — remove a task from the queue."""
    if r := _check_rate(request, cost=1):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    task_id = request.match_info["id"]

    for i, t in enumerate(manager.task_queue):
        if t.id == task_id:
            manager.task_queue.pop(i)
            await hub.broadcast({"type": "queue_update", "queue": [t.to_dict() for t in manager.task_queue]})
            return web.json_response({"status": "removed"})
    return web.json_response({"error": "Task not found in queue"}, status=404)


# ── Scratchpad endpoints ──

async def get_scratchpad(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    project_id = request.query.get("project_id")
    if not project_id:
        return web.json_response({"error": "project_id query parameter is required"}, status=400)
    entries = await db.get_scratchpad(project_id)
    return web.json_response(entries)


async def upsert_scratchpad(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    project_id = data.get("project_id")
    key = data.get("key")
    value = data.get("value", "")
    set_by = data.get("set_by", "")

    if not project_id or not key:
        return web.json_response({"error": "project_id and key are required"}, status=400)

    await db.upsert_scratchpad(project_id, key[:200], value[:10000], set_by[:100])

    # Broadcast scratchpad update to connected clients (Wave 5)
    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({
        "type": "scratchpad_update",
        "project_id": project_id,
        "key": key[:200],
        "value": value[:10000],
        "set_by": set_by[:100],
    })

    return web.json_response({"status": "ok", "key": key})


async def delete_scratchpad_entry(request: web.Request) -> web.Response:
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    key = request.match_info["key"]
    project_id = request.query.get("project_id")
    if not project_id:
        return web.json_response({"error": "project_id query parameter is required"}, status=400)
    success = await db.delete_scratchpad(project_id, key)
    if success:
        return web.json_response({"status": "deleted"})
    return web.json_response({"error": "Entry not found"}, status=404)


# ── Bookmark endpoints ──


# ── Config Import/Export endpoints ──

async def export_config(request: web.Request) -> web.Response:
    """GET /api/config/export — download full ashlr.yaml as JSON."""
    if r := _check_rate(request, cost=1):
        return r
    config_path = ASHLR_DIR / "ashlr.yaml"
    if not config_path.exists():
        return web.json_response({"error": "Config file not found"}, status=404)
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return web.json_response(raw, headers={
            "Content-Disposition": "attachment; filename=ashlr_config.json"
        })
    except Exception as e:
        return web.json_response({"error": f"Failed to read config: {e}"}, status=500)


async def import_config(request: web.Request) -> web.Response:
    """POST /api/config/import — import config from JSON, validate, apply."""
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "Config must be a JSON object"}, status=400)

    # Allowlist safe config sections — never allow backends, server, or auth_token
    _SAFE_IMPORT_KEYS = {"display", "agents", "llm", "voice", "licensing"}
    unsafe_keys = set(data.keys()) - _SAFE_IMPORT_KEYS
    if unsafe_keys:
        return web.json_response(
            {"error": f"Import not allowed for sections: {', '.join(sorted(unsafe_keys))}. "
             f"Allowed: {', '.join(sorted(_SAFE_IMPORT_KEYS))}"},
            status=400,
        )
    # Strip dangerous nested fields even within allowed sections
    if isinstance(data.get("agents"), dict):
        data["agents"].pop("backends", None)

    # Read current config for diff
    config_path = ASHLR_DIR / "ashlr.yaml"
    current = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                current = yaml.safe_load(f) or {}
        except Exception as e:
            log.debug(f"Failed to load current config for diff preview: {e}")

    # Build diff for preview
    def flat_diff(old: dict, new: dict, prefix: str = "") -> list[dict]:
        changes = []
        all_keys = set(list(old.keys()) + list(new.keys()))
        for k in sorted(all_keys):
            path = f"{prefix}.{k}" if prefix else k
            ov = old.get(k)
            nv = new.get(k)
            if isinstance(ov, dict) and isinstance(nv, dict):
                changes.extend(flat_diff(ov, nv, path))
            elif ov != nv:
                changes.append({"key": path, "old": ov, "new": nv})
        return changes

    diff = flat_diff(current, data)

    # Validate before writing — try loading the imported data as config
    tmp_validate = config_path.with_suffix(".yaml.validate")
    try:
        with open(tmp_validate, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        # Attempt to parse — load_config reads from the standard path,
        # so we validate structure manually
        test_raw = deep_merge(DEFAULT_CONFIG.copy(), data)
        # Check critical sections exist and have valid types
        if not isinstance(test_raw.get("server", {}), dict):
            raise ValueError("'server' must be an object")
        if not isinstance(test_raw.get("agents", {}), dict):
            raise ValueError("'agents' must be an object")
    except ValueError as e:
        return web.json_response({"error": f"Invalid config structure: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Config validation failed: {e}"}, status=400)
    finally:
        tmp_validate.unlink(missing_ok=True)

    # Strip security-sensitive fields from import payload
    if isinstance(data.get("server"), dict):
        data["server"].pop("auth_token", None)
        data["server"].pop("host", None)

    # Atomic write (validated)
    async with _config_write_lock:
        try:
            tmp_path = config_path.with_suffix(".yaml.tmp")
            def _write_import():
                with open(tmp_path, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                tmp_path.rename(config_path)
            await asyncio.to_thread(_write_import)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            return web.json_response({"error": f"Failed to write config: {e}"}, status=500)

    # Reload in-memory config
    config: Config = request.app["config"]
    try:
        reloaded = load_config(bool(shutil.which("claude")))
        for attr in ("max_agents", "default_role", "default_working_dir", "output_capture_interval",
                      "memory_limit_mb", "default_backend", "llm_enabled", "llm_model",
                      "llm_summary_interval", "voice_feedback", "idle_agent_ttl",
                      "health_low_threshold", "health_critical_threshold",
                      "stall_timeout_minutes", "hung_timeout_minutes"):
            if hasattr(reloaded, attr):
                setattr(config, attr, getattr(reloaded, attr))
    except Exception as e:
        log.warning(f"Config reload partial failure: {e}")

    return web.json_response({"status": "imported", "changes": diff})


# ── Extension Discovery endpoints ──

async def get_extensions(request: web.Request) -> web.Response:
    """GET /api/extensions — return cached extension scan results."""
    if r := _check_rate(request, cost=1):
        return r
    scanner: ExtensionScanner = request.app["extension_scanner"]
    return web.json_response(scanner.to_dict())


async def refresh_extensions(request: web.Request) -> web.Response:
    """POST /api/extensions/refresh — re-scan filesystem for extensions."""
    if r := _check_rate(request, cost=3, rate=1, burst=5):
        return r
    scanner: ExtensionScanner = request.app["extension_scanner"]
    # Gather project dirs from DB + active agents
    project_dirs: set[str] = set()
    db: Database = request.app["db"]
    if db:
        try:
            projects = await db.get_projects()
            for p in projects:
                pdir = p.get("path", "")
                if pdir:
                    project_dirs.add(pdir)
        except Exception as e:
            log.error(f"Failed to get project paths from DB: {e}")
    manager: AgentManager = request.app["agent_manager"]
    for agent in list(manager.agents.values()):
        if agent.working_dir:
            project_dirs.add(agent.working_dir)
    result = scanner.scan(list(project_dirs))
    return web.json_response(result)


# ── History endpoints ──

async def list_history(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)
    # Clamp limit to max 1000
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    history = await db.get_agent_history(limit, offset)
    total = await db.get_agent_history_count()
    return web.json_response({
        "data": history,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


async def get_history_item(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    agent_id = request.match_info["id"]
    item = await db.get_agent_history_item(agent_id)
    if not item:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(item)


# ── File Conflicts endpoint ──

async def list_conflicts(request: web.Request) -> web.Response:
    """GET /api/conflicts — current file conflicts between active agents."""
    manager: AgentManager = request.app["agent_manager"]
    conflicts: list[dict] = []
    seen: set[tuple] = set()
    for file_path, agent_ops in manager.file_activity.items():
        writers = [(aid, op) for aid, op in agent_ops.items()
                   if op == "write" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        if len(writers) < 2:
            continue
        for i, (a1, _) in enumerate(writers):
            for a2, _ in writers[i + 1:]:
                key = tuple(sorted([a1, a2])) + (file_path,)
                if key in seen:
                    continue
                seen.add(key)
                ag1, ag2 = manager.agents[a1], manager.agents[a2]
                conflicts.append({
                    "file_path": file_path,
                    "agents": [
                        {"id": a1, "name": ag1.name, "role": ag1.role, "status": ag1.status},
                        {"id": a2, "name": ag2.name, "role": ag2.role, "status": ag2.status},
                    ],
                    "severity": "conflict",
                })
    # Also include read-write overlaps as warnings
    for file_path, agent_ops in manager.file_activity.items():
        writers = [aid for aid, op in agent_ops.items()
                   if op == "write" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        readers = [aid for aid, op in agent_ops.items()
                   if op == "read" and aid in manager.agents
                   and manager.agents[aid].status in ("working", "planning", "reading")]
        for w in writers:
            for r in readers:
                key = tuple(sorted([w, r])) + (file_path,)
                if key in seen:
                    continue
                seen.add(key)
                ag_w, ag_r = manager.agents[w], manager.agents[r]
                conflicts.append({
                    "file_path": file_path,
                    "agents": [
                        {"id": w, "name": ag_w.name, "role": ag_w.role, "status": ag_w.status},
                        {"id": r, "name": ag_r.name, "role": ag_r.role, "status": ag_r.status},
                    ],
                    "severity": "warning",
                })
    # Sort: conflicts first, then warnings
    conflicts.sort(key=lambda c: (0 if c["severity"] == "conflict" else 1, c["file_path"]))
    # File activity summary
    active_files = {}
    for fp, ops in manager.file_activity.items():
        active_agents = [aid for aid in ops if aid in manager.agents]
        if active_agents:
            active_files[fp] = {
                "operations": {aid: ops[aid] for aid in active_agents},
                "agent_count": len(active_agents),
            }
    return web.json_response({
        "conflicts": conflicts,
        "total": len(conflicts),
        "active_files": dict(sorted(active_files.items(), key=lambda x: -x[1]["agent_count"])[:50]),
    })


# ── Activity Events endpoint ──

async def list_events(request: web.Request) -> web.Response:
    """GET /api/events — list activity events with optional filters."""
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    try:
        limit = int(request.query.get("limit", 100))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return web.json_response({"error": "limit and offset must be integers"}, status=400)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    agent_id = request.query.get("agent_id")
    event_type = request.query.get("event_type")
    since = request.query.get("since")

    # Project filter: find agent_ids belonging to this project (active + archived)
    project_id = request.query.get("project_id")
    if project_id and not agent_id:
        # Check active in-memory agents
        project_agent_ids = [
            aid for aid, a in list(manager.agents.items())
            if getattr(a, "project_id", None) == project_id
        ]
        # Also check archived agents in DB
        try:
            archived = await db.get_agents_by_project(project_id)
            for rec in archived:
                aid = rec.get("id", "")
                if aid and aid not in project_agent_ids:
                    project_agent_ids.append(aid)
        except Exception:
            pass  # DB method may not exist yet, fall back to active-only
        if not project_agent_ids:
            return web.json_response({"data": [], "pagination": {"limit": limit, "offset": offset, "total": 0}})
        # Fetch events for each agent and merge
        all_events = []
        for aid in project_agent_ids[:20]:  # Cap to prevent huge queries
            evts = await db.get_events(limit, 0, aid, event_type, since)
            all_events.extend(evts)
        # Sort by created_at descending, apply pagination
        all_events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        total = len(all_events)
        paginated = all_events[offset:offset + limit]
        return web.json_response({"data": paginated, "pagination": {"limit": limit, "offset": offset, "total": total}})

    events = await db.get_events(limit, offset, agent_id, event_type, since)
    total = await db.get_events_count(agent_id, event_type, since)
    return web.json_response({
        "data": events,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    })


async def export_events(request: web.Request) -> web.Response:
    """GET /api/events/export — export events as CSV or JSON."""
    if r := _check_rate(request, cost=3, rate=0.2, burst=3):
        return r
    db: Database = request.app["db"]
    fmt = request.query.get("format", "json")
    agent_id = request.query.get("agent_id")
    event_type = request.query.get("event_type")
    since = request.query.get("since")

    events = await db.get_events(10000, 0, agent_id, event_type, since)

    if fmt == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "event_type", "agent_id", "agent_name", "message", "created_at"])
        for e in events:
            writer.writerow([
                e.get("id", ""), e.get("event_type", ""), e.get("agent_id", ""),
                e.get("agent_name", ""), e.get("message", ""), e.get("created_at", ""),
            ])
        return web.Response(
            text=output.getvalue(),
            content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=ashlr_events.csv"},
        )
    else:
        return web.json_response(events, headers={
            "Content-Disposition": "attachment; filename=ashlr_events.json",
        })


# ── Agent PATCH endpoint (Wave 3A) ──

async def patch_agent(request: web.Request) -> web.Response:
    """PATCH /api/agents/{id} — update agent fields (name, task, project_id)."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    if err := _check_agent_ownership(request, agent):
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not data:
        return web.json_response({"error": "No fields to update"}, status=400)

    errors = []

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str):
            errors.append("name must be a string")
        else:
            name = re.sub(r'[\x00-\x1f]', '', name).strip()[:100]
            if not name:
                errors.append("name cannot be empty")
            else:
                agent.name = name

    if "task" in data:
        task = data["task"]
        if not isinstance(task, str):
            errors.append("task must be a string")
        elif len(task) > 10000:
            errors.append("task exceeds 10000 character limit")
        else:
            agent.task = task

    if "project_id" in data:
        project_id = data["project_id"]
        if project_id is not None and not isinstance(project_id, str):
            errors.append("project_id must be a string or null")
        else:
            agent.project_id = project_id

    if errors:
        return web.json_response({"error": "; ".join(errors)}, status=400)

    agent.updated_at = datetime.now(timezone.utc).isoformat()
    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})

    return web.json_response(agent.to_dict())


# ── Auto-handoff configuration (Wave 5) ──

async def configure_handoff(request: web.Request) -> web.Response:
    """POST /api/agents/{id}/configure-handoff — set next_agent_config for auto-handoff."""
    if r := _check_rate(request, cost=2):
        return r
    manager: AgentManager = request.app["agent_manager"]
    agent_id = request.match_info["id"]

    agent = manager.agents.get(agent_id)
    if not agent:
        return web.json_response({"error": "Agent not found"}, status=404)

    if err := _check_agent_ownership(request, agent):
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    next_config = data.get("next_agent_config")
    if next_config is not None and not isinstance(next_config, dict):
        return web.json_response({"error": "next_agent_config must be a dict or null"}, status=400)

    if next_config:
        # Validate required fields
        if not next_config.get("task"):
            return web.json_response({"error": "next_agent_config.task is required"}, status=400)
        # Validate working_dir if provided
        wd = next_config.get("working_dir", "")
        if wd:
            resolved_wd = os.path.realpath(os.path.expanduser(wd))
            home = str(Path.home())
            if not (resolved_wd.startswith(home) or resolved_wd.startswith("/tmp") or resolved_wd.startswith("/private/tmp")):
                return web.json_response({"error": "working_dir must be under home directory or /tmp"}, status=400)
            wd = resolved_wd
        # Sanitize
        next_config = {
            "role": next_config.get("role", "general"),
            "task": next_config["task"][:10000],
            "backend": next_config.get("backend", ""),
            "model": next_config.get("model", ""),
            "name": next_config.get("name", ""),
            "working_dir": wd,
        }

    agent.next_agent_config = next_config
    agent.updated_at = datetime.now(timezone.utc).isoformat()
    return web.json_response({"status": "ok", "agent_id": agent_id, "next_agent_config": next_config})


# ── Project scratchpad alias (Wave 5) ──

async def get_project_scratchpad(request: web.Request) -> web.Response:
    """GET /api/projects/{id}/scratchpad — convenience alias."""
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    entries = await db.get_scratchpad(project_id)
    return web.json_response(entries)


async def upsert_project_scratchpad(request: web.Request) -> web.Response:
    """POST /api/projects/{id}/scratchpad — convenience alias."""
    if r := _check_rate(request, cost=2):
        return r
    db: Database = request.app["db"]
    project_id = request.match_info["id"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    key = data.get("key", "").strip()
    value = data.get("value", "").strip()
    set_by = data.get("set_by", "user").strip()
    if not key or not value:
        return web.json_response({"error": "key and value required"}, status=400)
    await db.upsert_scratchpad(project_id, key[:200], value[:10000], set_by[:100])
    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({
        "type": "scratchpad_update",
        "project_id": project_id,
        "key": key[:200],
        "value": value[:10000],
        "set_by": set_by[:100],
    })
    return web.json_response({"status": "ok"})


async def get_project_events(request: web.Request) -> web.Response:
    """GET /api/projects/{id}/events — events for agents in a project."""
    if r := _check_rate(request, cost=1):
        return r
    db: Database = request.app["db"]
    manager: AgentManager = request.app["agent_manager"]
    project_id = request.match_info["id"]
    limit = min(int(request.query.get("limit", "100")), 500)
    since = request.query.get("since")

    # Find agent IDs belonging to this project
    project_agent_ids = [
        aid for aid, agent in list(manager.agents.items())
        if agent.project_id == project_id
    ]

    # Gather events for each project agent
    all_events = []
    for aid in project_agent_ids:
        events = await db.get_events(limit=limit, agent_id=aid, since=since)
        all_events.extend(events)

    # Sort by created_at descending and limit
    all_events.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return web.json_response(all_events[:limit])


# ── Bulk operations endpoint (Wave 3B) ──

async def bulk_agent_action(request: web.Request) -> web.Response:
    """POST /api/agents/bulk — perform bulk kill/pause/resume on multiple agents."""
    if r := _check_rate(request, cost=2, rate=1.0, burst=5):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    action = data.get("action")
    agent_ids = data.get("agent_ids", [])

    message = data.get("message", "")

    if action not in ("kill", "pause", "resume", "send", "restart"):
        return web.json_response({"error": f"Invalid action '{action}'. Must be 'kill', 'pause', 'resume', 'send', or 'restart'"}, status=400)

    if action == "send" and (not isinstance(message, str) or not message.strip()):
        return web.json_response({"error": "Bulk send requires a non-empty 'message' field"}, status=400)

    if not isinstance(agent_ids, list) or not agent_ids:
        return web.json_response({"error": "agent_ids must be a non-empty list"}, status=400)

    success_ids = []
    failed_items = []

    for aid in agent_ids:
        if not isinstance(aid, str):
            failed_items.append({"id": str(aid), "error": "Invalid agent ID type"})
            continue

        agent = manager.agents.get(aid)
        if not agent:
            failed_items.append({"id": aid, "error": "Agent not found"})
            continue

        # Ownership check — non-admin users can only act on their own agents
        if err := _check_agent_ownership(request, agent):
            failed_items.append({"id": aid, "error": "Permission denied"})
            continue

        try:
            if action == "kill":
                # Archive to history before killing
                try:
                    await db.save_agent(agent)
                except Exception as e:
                    log.warning(f"Failed to archive agent {aid} during bulk kill: {e}")
                name = agent.name
                ok = await manager.kill(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_removed", "agent_id": aid})
                    await hub.broadcast_event("agent_killed", f"Agent {name} killed (bulk)", aid, name)
                else:
                    failed_items.append({"id": aid, "error": "Kill failed"})

            elif action == "pause":
                ok = await manager.pause(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Pause failed"})

            elif action == "resume":
                ok = await manager.resume(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Resume failed"})

            elif action == "send":
                sanitized = message[:500].replace('\r', '')
                sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)
                ok = await manager.send_message(aid, sanitized)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Send failed"})

            elif action == "restart":
                ok = await manager.restart(aid)
                if ok:
                    success_ids.append(aid)
                    await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
                else:
                    failed_items.append({"id": aid, "error": "Restart failed"})

        except Exception as e:
            failed_items.append({"id": aid, "error": redact_secrets(str(e))})

    return web.json_response({"success": success_ids, "failed": failed_items})


async def batch_spawn(request: web.Request) -> web.Response:
    """POST /api/agents/batch-spawn — spawn multiple agents from a single request.

    Body: {"agents": [{"role": "backend", "name": "api", "task": "...", "working_dir": "...", ...}, ...]}
    Optional: "project_id", "plan_mode" at top level to apply to all agents.
    """
    if r := _check_rate(request, cost=5, rate=0.5, burst=3):
        return r
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    config: Config = request.app["config"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    agent_specs = data.get("agents", [])
    if not isinstance(agent_specs, list) or not agent_specs:
        return web.json_response({"error": "agents must be a non-empty list"}, status=400)
    if len(agent_specs) > 10:
        return web.json_response({"error": "Maximum 10 agents per batch"}, status=400)

    shared_project = data.get("project_id")
    shared_plan_mode = data.get("plan_mode", False)
    shared_dir = data.get("working_dir")

    # Feature gate: fleet_presets requires Pro
    if r := _check_feature(request, "fleet_presets"):
        return r

    # Check concurrent agent limit before spawning
    current_count = len(manager.agents)
    max_agents = _effective_max_agents(request.app)
    available_slots = max_agents - current_count
    if available_slots <= 0:
        suffix = " Upgrade to Pro for more." if not request.app.get("license", COMMUNITY_LICENSE).is_pro else ""
        return web.json_response({"error": f"Agent limit reached ({max_agents}).{suffix}"}, status=409)
    if len(agent_specs) > available_slots:
        return web.json_response(
            {"error": f"Only {available_slots} agent slots available, but {len(agent_specs)} requested"},
            status=409,
        )

    spawned = []
    failed = []

    for i, spec in enumerate(agent_specs):
        if not isinstance(spec, dict):
            failed.append({"index": i, "error": "Each agent spec must be an object"})
            continue
        task = spec.get("task", "")
        if not task:
            failed.append({"index": i, "error": "task is required"})
            continue
        role = spec.get("role", config.default_role)
        if role not in BUILTIN_ROLES:
            failed.append({"index": i, "error": f"Unknown role '{role}'"})
            continue

        try:
            agent = await manager.spawn(
                role=role,
                name=spec.get("name"),
                working_dir=spec.get("working_dir") or shared_dir,
                task=task,
                plan_mode=spec.get("plan_mode", shared_plan_mode),
                backend=spec.get("backend", config.default_backend),
                model=spec.get("model"),
                tools=spec.get("tools"),
            )
            if shared_project or spec.get("project_id"):
                agent.project_id = spec.get("project_id") or shared_project
            spawned.append(agent.to_dict())
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
            await hub.broadcast_event("agent_spawned", f"Batch spawned: {agent.name}", agent.id, agent.name)
        except Exception as e:
            failed.append({"index": i, "error": redact_secrets(str(e))})

    return web.json_response({
        "spawned": spawned,
        "failed": failed,
        "total_spawned": len(spawned),
        "total_failed": len(failed),
    }, status=201 if spawned else 400)


async def agent_suggestions(request: web.Request) -> web.Response:
    """GET /api/agents/suggestions?task=... — find similar past tasks with their outcomes."""
    db: Database = request.app["db"]
    task_query = request.query.get("task", "").strip()
    if not task_query or len(task_query) < 5:
        return web.json_response({"suggestions": [], "message": "Provide at least 5 characters in task query"})
    try:
        results = await db.find_similar_tasks(task_query, limit=5)
        suggestions = []
        for r in results:
            suggestions.append({
                "role": r.get("role"),
                "backend": r.get("backend"),
                "task": r.get("task"),
                "status": r.get("status"),
                "duration_sec": r.get("duration_sec"),
                "cost_usd": round(r.get("estimated_cost_usd") or 0, 4),
                "completed_at": r.get("completed_at"),
                "success": r.get("status") not in ("error",),
            })
        return web.json_response({"suggestions": suggestions, "query": task_query})
    except Exception as e:
        log.error(f"Agent suggestions error: {e}")
        return web.json_response({"suggestions": [], "error": redact_secrets(str(e))})


async def bulk_respond(request: web.Request) -> web.Response:
    """Send responses to multiple waiting agents at once."""
    manager: AgentManager = request.app["agent_manager"]
    hub: WebSocketHub = request.app["ws_hub"]
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    responses = data.get("responses", [])
    if not responses or not isinstance(responses, list):
        return web.json_response({"error": "responses must be a non-empty list"}, status=400)

    success_ids = []
    failed_items = []

    for item in responses:
        if not isinstance(item, dict):
            failed_items.append({"id": "", "error": "Each response must be an object"})
            continue
        aid = item.get("agent_id", "")
        message = item.get("message", "")
        if not isinstance(message, str) or not message:
            failed_items.append({"id": aid, "error": "Missing or invalid message (must be a string)"})
            continue
        if not aid:
            failed_items.append({"id": aid, "error": "Missing agent_id"})
            continue

        agent = manager.agents.get(aid)
        if not agent:
            failed_items.append({"id": aid, "error": "Agent not found"})
            continue

        # Sanitize message
        message = message[:500].replace('\r', '')
        message = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', message)

        try:
            await manager.send_message(aid, message)
            success_ids.append(aid)
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        except Exception as e:
            failed_items.append({"id": aid, "error": redact_secrets(str(e))})

    return web.json_response({"success": success_ids, "failed": failed_items})

# Background tasks moved to ashlr_ao.background — re-exported above



# ─────────────────────────────────────────────
# License API Endpoints
# ─────────────────────────────────────────────

async def license_status(request: web.Request) -> web.Response:
    """GET /api/license/status — current license info (public)."""
    lic: License = request.app.get("license", COMMUNITY_LICENSE)
    gated = {}
    for feat in sorted(PRO_FEATURES):
        gated[feat] = lic.is_pro  # True if unlocked
    return web.json_response({
        "license": lic.to_dict(),
        "effective_max_agents": _effective_max_agents(request.app),
        "gated_features": gated,
    })


async def activate_license(request: web.Request) -> web.Response:
    """POST /api/license/activate — activate a license key (admin-only when auth enabled)."""
    config: Config = request.app["config"]
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = str(data.get("license_key", "")).strip()
    if not key:
        return web.json_response({"error": "license_key is required"}, status=400)

    lic = validate_license(key)
    if not lic.is_pro:
        return web.json_response({"error": "Invalid or expired license key"}, status=400)

    # Persist to DB (org) if auth is enabled
    db: Database = request.app["db"]
    if config.require_auth:
        user = request.get("user")
        if user and user.org_id:
            await db.update_org_license(user.org_id, key, lic.tier)

    # Persist to config YAML
    try:
        config_path = ASHLR_DIR / "ashlr.yaml"
        if config_path.exists():
            with open(config_path) as f:
                raw_yaml = yaml.safe_load(f) or {}
        else:
            raw_yaml = {}
        raw_yaml.setdefault("licensing", {})["key"] = key
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".yaml.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
            Path(tmp_path).rename(config_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
    except Exception as e:
        log.warning(f"Failed to persist license key to config: {e}")

    # Update runtime state
    request.app["license"] = lic
    manager: AgentManager = request.app["agent_manager"]
    manager.license = lic
    config.license_key = key
    log.info(f"License activated: tier={lic.tier}, max_agents={lic.max_agents}, expires={lic.expires_at}")

    # Broadcast to all WS clients
    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({"type": "license_update", "license": lic.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})

    return web.json_response({"license": lic.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})


async def deactivate_license(request: web.Request) -> web.Response:
    """DELETE /api/license/deactivate — remove license and revert to Community."""
    config: Config = request.app["config"]
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required"}, status=403)

    # Clear from DB
    db: Database = request.app["db"]
    if config.require_auth:
        user = request.get("user")
        if user and user.org_id:
            await db.update_org_license(user.org_id, "", "community")

    # Clear from config YAML
    try:
        config_path = ASHLR_DIR / "ashlr.yaml"
        if config_path.exists():
            with open(config_path) as f:
                raw_yaml = yaml.safe_load(f) or {}
            raw_yaml.setdefault("licensing", {})["key"] = ""
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".yaml.tmp")
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
                Path(tmp_path).rename(config_path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
    except Exception as e:
        log.warning(f"Failed to clear license key from config: {e}")

    # Revert runtime state
    request.app["license"] = COMMUNITY_LICENSE
    manager: AgentManager = request.app["agent_manager"]
    manager.license = COMMUNITY_LICENSE
    config.license_key = ""
    log.info("License deactivated — reverted to Community")

    hub: WebSocketHub = request.app["ws_hub"]
    await hub.broadcast({"type": "license_update", "license": COMMUNITY_LICENSE.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})

    return web.json_response({"license": COMMUNITY_LICENSE.to_dict(), "effective_max_agents": _effective_max_agents(request.app)})


def create_app(config: Config) -> web.Application:
    middlewares = [security_headers_middleware, request_logging_middleware, compression_middleware, rate_limit_middleware]
    if config.require_auth:
        middlewares.append(auth_middleware)
    app = web.Application(middlewares=middlewares, client_max_size=10 * 1024 * 1024)
    app["config"] = config
    app["_start_time"] = time.monotonic()

    manager = AgentManager(config)
    app["agent_manager"] = manager
    app["license"] = COMMUNITY_LICENSE

    db = Database()
    app["db"] = db
    app["db_available"] = True  # Set to False if DB init fails in start_background_tasks
    manager.db = db  # Give manager DB ref for cleanup operations (e.g., release file locks on kill)

    hub = WebSocketHub(manager, config, db)
    hub.app = app
    app["ws_hub"] = hub

    # Rate limiter
    rate_limiter = RateLimiter()
    app["rate_limiter"] = rate_limiter

    # Unified Intelligence Client (xAI Grok)
    intel_client = IntelligenceClient(config)
    app["intelligence"] = intel_client
    if intel_client.available:
        log.info(f"Intelligence enabled: xai/{config.llm_model}")
    else:
        log.info("Intelligence disabled (set XAI_API_KEY and llm.enabled in config)")

    # Compile alert patterns for pattern alerting in capture loop
    compiled_alerts = []
    for ap in config.alert_patterns:
        try:
            compiled_alerts.append((re.compile(ap["pattern"]), ap.get("severity", "warning"), ap.get("label", "Alert")))
        except (re.error, KeyError) as e:
            log.warning(f"Skipping invalid alert pattern: {ap} — {e}")
    app["_compiled_alert_patterns"] = compiled_alerts if compiled_alerts else None
    if compiled_alerts:
        log.info(f"Pattern alerting enabled: {len(compiled_alerts)} patterns")
    app["intelligence_insights"] = []  # list[AgentInsight]
    app["_meta_state_hash"] = ""  # For skipping unchanged fleet analysis


    # Extension Scanner — initial scan at startup
    scanner = ExtensionScanner()
    scanner.scan()  # Initial scan with no project dirs (DB not ready yet)
    app["extension_scanner"] = scanner

    # Routes
    app.router.add_get("/", serve_dashboard)
    app.router.add_get("/logo.png", serve_logo)
    app.router.add_get("/ws", hub.handle_ws)

    # REST API — Agents (bulk + patch BEFORE {id} catch-all routes)
    app.router.add_get("/api/agents", list_agents)
    app.router.add_post("/api/agents", spawn_agent)
    app.router.add_post("/api/agents/bulk", bulk_agent_action)
    app.router.add_post("/api/agents/bulk-respond", bulk_respond)
    app.router.add_post("/api/agents/batch-spawn", batch_spawn)
    app.router.add_post("/api/agents/validate", validate_spawn)
    app.router.add_get("/api/agents/suggestions", agent_suggestions)
    app.router.add_patch("/api/agents/{id}", patch_agent)
    app.router.add_get("/api/agents/{id}", get_agent)
    app.router.add_delete("/api/agents/{id}", delete_agent)
    app.router.add_post("/api/agents/{id}/send", send_to_agent)
    app.router.add_post("/api/agents/{id}/pause", pause_agent)
    app.router.add_post("/api/agents/{id}/resume", resume_agent)
    app.router.add_post("/api/agents/{id}/restart", restart_agent)
    app.router.add_get("/api/agents/{id}/output", get_agent_output)
    app.router.add_get("/api/agents/{id}/full-output", get_agent_full_output)
    app.router.add_post("/api/agents/{id}/summarize", generate_summary)
    app.router.add_post("/api/agents/{id}/message", send_agent_message)
    app.router.add_get("/api/agents/{id}/messages", get_agent_messages)
    app.router.add_post("/api/agents/{id}/handoff", handoff_agent)
    app.router.add_put("/api/agents/{id}/notes", update_agent_notes)
    app.router.add_put("/api/agents/{id}/tags", update_agent_tags)
    app.router.add_get("/api/agents/{id}/bookmarks", list_agent_bookmarks)
    app.router.add_post("/api/agents/{id}/bookmarks", add_agent_bookmark)
    app.router.add_delete("/api/agents/{id}/bookmarks/{bid}", delete_agent_bookmark)
    app.router.add_get("/api/agents/{id}/output-history", get_agent_output_history)
    app.router.add_get("/api/agents/{id}/output-search", search_agent_output)
    app.router.add_get("/api/agents/{id}/activity", get_agent_activity)
    app.router.add_get("/api/agents/{id}/tool-invocations", get_agent_tool_invocations)
    app.router.add_get("/api/agents/{id}/file-operations", get_agent_file_operations)
    app.router.add_get("/api/agents/{id}/output/export", export_agent_output)
    app.router.add_get("/api/agents/{id}/output/search", search_agent_output)
    app.router.add_get("/api/agents/{id}/snapshots", get_agent_snapshots)
    app.router.add_post("/api/agents/{id}/snapshots", create_agent_snapshot)

    # REST API — Search
    app.router.add_get("/api/search", search_agents)

    # REST API — Intelligence
    app.router.add_get("/api/intelligence/insights", get_intelligence_insights)
    app.router.add_post("/api/intelligence/insights/{id}/ack", acknowledge_insight)
    app.router.add_post("/api/intelligence/command", intelligence_command)

    # REST API — Analytics
    app.router.add_get("/api/analytics", fleet_analytics)
    app.router.add_get("/api/collaboration", collaboration_graph)

    # REST API — System
    app.router.add_get("/api/health", health_check)
    app.router.add_get("/api/health/detailed", health_detailed)
    app.router.add_post("/api/diagnostic", run_diagnostic)
    app.router.add_get("/api/stats", get_server_stats)
    app.router.add_get("/api/system", system_metrics)
    app.router.add_get("/api/roles", list_roles)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/backends", list_backends)
    app.router.add_post("/api/auth/verify", verify_auth)
    app.router.add_get("/api/auth/status", auth_status)
    app.router.add_post("/api/auth/register", auth_register)
    app.router.add_post("/api/auth/login", auth_login)
    app.router.add_post("/api/auth/logout", auth_logout)
    app.router.add_get("/api/auth/me", auth_me)
    app.router.add_post("/api/auth/invite", auth_invite)
    app.router.add_get("/api/auth/team", auth_team)
    app.router.add_get("/api/costs", get_costs)
    app.router.add_get("/api/conflicts", list_conflicts)

    # REST API — Projects
    app.router.add_get("/api/projects", list_projects)
    app.router.add_post("/api/projects", create_project)
    app.router.add_get("/api/projects/{id}/context", get_project_context)
    app.router.add_delete("/api/projects/{id}", delete_project)
    app.router.add_put("/api/projects/{id}", update_project)

    # REST API — GitHub Integration
    app.router.add_get("/api/github/status", github_status)
    app.router.add_get("/api/projects/{id}/github", github_project_info)
    app.router.add_post("/api/projects/{id}/github/issues", github_create_issue)
    app.router.add_post("/api/projects/{id}/github/pulls", github_create_pr)

    # REST API — Workflows
    app.router.add_get("/api/workflows", list_workflows)
    app.router.add_post("/api/workflows", create_workflow)
    app.router.add_put("/api/workflows/{id}", update_workflow)
    app.router.add_post("/api/workflows/{id}/run", run_workflow)
    app.router.add_delete("/api/workflows/{id}", delete_workflow)

    # REST API — Workflow Runs (pipeline tracking)
    app.router.add_get("/api/workflow-runs", list_workflow_runs)
    app.router.add_get("/api/workflow-runs/{id}", get_workflow_run)
    app.router.add_post("/api/workflow-runs/{id}/cancel", cancel_workflow_run)

    # REST API — History & Events
    app.router.add_get("/api/history", list_history)
    app.router.add_get("/api/history/{id}", get_history_item)
    app.router.add_get("/api/events", list_events)
    app.router.add_get("/api/events/export", export_events)

    # REST API — Presets
    app.router.add_get("/api/presets", list_presets)
    app.router.add_post("/api/presets", create_preset)
    app.router.add_put("/api/presets/{id}", update_preset)
    app.router.add_delete("/api/presets/{id}", delete_preset)

    # REST API — Fleet Templates
    app.router.add_get("/api/fleet-templates", list_fleet_templates)
    app.router.add_post("/api/fleet-templates", create_fleet_template)
    app.router.add_get("/api/fleet-templates/{id}", get_fleet_template)
    app.router.add_put("/api/fleet-templates/{id}", update_fleet_template)
    app.router.add_delete("/api/fleet-templates/{id}", delete_fleet_template)
    app.router.add_post("/api/fleet-templates/{id}/deploy", deploy_fleet_template)

    # REST API — Webhooks
    app.router.add_get("/api/webhooks", list_webhooks)
    app.router.add_post("/api/webhooks", create_webhook)
    app.router.add_put("/api/webhooks/{id}", update_webhook)
    app.router.add_delete("/api/webhooks/{id}", delete_webhook)
    app.router.add_post("/api/webhooks/{id}/test", test_webhook)
    app.router.add_get("/api/webhooks/{id}/deliveries", get_webhook_deliveries)

    # REST API — Agent Clone + Handoff
    app.router.add_post("/api/agents/{id}/clone", clone_agent)
    app.router.add_post("/api/agents/{id}/configure-handoff", configure_handoff)

    # REST API — Project Scratchpad + Events alias
    app.router.add_get("/api/projects/{id}/scratchpad", get_project_scratchpad)
    app.router.add_post("/api/projects/{id}/scratchpad", upsert_project_scratchpad)
    app.router.add_get("/api/projects/{id}/events", get_project_events)

    # REST API — Session Resume
    app.router.add_get("/api/sessions/resumable", get_resumable_sessions)
    app.router.add_post("/api/sessions/{id}/resume", resume_from_history)

    # REST API — Fleet Export
    app.router.add_get("/api/fleet/export", export_fleet_state)

    # REST API — Task Queue
    app.router.add_get("/api/queue", list_queue)
    app.router.add_post("/api/queue", add_to_queue)
    app.router.add_delete("/api/queue/{id}", remove_from_queue)

    # REST API — Scratchpad
    app.router.add_get("/api/scratchpad", get_scratchpad)
    app.router.add_put("/api/scratchpad", upsert_scratchpad)
    app.router.add_delete("/api/scratchpad/{key}", delete_scratchpad_entry)

    # Note: bookmark routes already registered at lines above (add_agent_bookmark, delete_agent_bookmark, list_agent_bookmarks)

    # REST API — Config Import/Export
    app.router.add_get("/api/config/export", export_config)
    app.router.add_post("/api/config/import", import_config)

    # REST API — Extensions
    app.router.add_get("/api/extensions", get_extensions)
    app.router.add_post("/api/extensions/refresh", refresh_extensions)

    # REST API — Licensing
    app.router.add_get("/api/license/status", license_status)
    app.router.add_post("/api/license/activate", activate_license)
    app.router.add_delete("/api/license/deactivate", deactivate_license)

    # Signal handlers (register in on_startup when event loop is running)
    async def _register_signals(app: web.Application) -> None:
        setup_signal_handlers(app["agent_manager"])
    app.on_startup.append(_register_signals)

    # Background tasks
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app


def setup_signal_handlers(agent_manager: AgentManager) -> None:
    """Register async-safe signal handlers using loop.add_signal_handler."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop yet — use synchronous signal.signal as fallback
        def handle_shutdown(signum: int, frame: Any) -> None:
            print("\n\033[33m→ Shutting down Ashlr...\033[0m")
            agent_manager.cleanup_all()
            raise KeyboardInterrupt()
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
        return

    _shutdown_requested = False

    def _signal_handler() -> None:
        nonlocal _shutdown_requested
        if _shutdown_requested:
            return
        _shutdown_requested = True
        print("\n\033[33m→ Shutting down Ashlr...\033[0m")
        asyncio.ensure_future(_async_shutdown(agent_manager))

    loop.add_signal_handler(signal.SIGINT, _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)


async def _async_shutdown(agent_manager: AgentManager) -> None:
    """Async-safe shutdown: runs cleanup then raises GracefulExit."""
    try:
        await asyncio.to_thread(agent_manager.cleanup_all)
        print("\033[32m✓ All agent sessions cleaned up\033[0m")
    except Exception as e:
        log.warning(f"Shutdown cleanup error: {e}")
    raise web.GracefulExit()


def main() -> None:
    from ashlr_ao import __version__

    parser = argparse.ArgumentParser(
        prog="ashlr",
        description="Ashlr AO — Agent Orchestration Platform",
    )
    parser.add_argument("-V", "--version", action="version", version=f"ashlr {__version__}")
    parser.add_argument("-p", "--port", type=int, help="server port (default: 5111)")
    parser.add_argument("-H", "--host", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--demo", action="store_true", help="force demo mode")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="log level")
    args = parser.parse_args()

    print_banner()
    has_claude = check_dependencies()

    # --demo flag forces demo mode via env var (same mechanism as CLAUDECODE)
    if args.demo:
        os.environ["CLAUDECODE"] = "1"

    config = load_config(has_claude)

    # CLI args > env vars > config file
    if args.port is not None:
        config.port = args.port
    if args.host is not None:
        config.host = args.host
    if args.log_level is not None:
        config.log_level = args.log_level

    setup_logging(config.log_level)

    app = create_app(config)

    # Clean up orphaned tmux sessions from prior ungraceful shutdowns
    manager = app["agent_manager"]
    orphans = manager.cleanup_orphaned_sessions()
    if orphans:
        print(f"  \033[33mCleaned up {orphans} orphaned tmux session{'s' if orphans != 1 else ''}\033[0m")

    mode_str = "\033[33mDEMO MODE\033[0m" if config.demo_mode else "\033[32mLIVE MODE\033[0m"
    print(f"  Mode: {mode_str}")
    print(f"  Dashboard: \033[36mhttp://{config.host}:{config.port}\033[0m")
    print(f"  Max agents: {config.max_agents}")
    print()

    try:
        web.run_app(app, host=config.host, port=config.port, print=None)
    except OSError as e:
        if "Address already in use" in str(e) or getattr(e, 'errno', None) == 48:
            log.error(f"Port {config.port} is already in use.")
            log.error(f"  → Set ASHLR_PORT=8080 to use a different port")
            log.error(f"  → On macOS, AirPlay Receiver may use port 5000 (Ashlr defaults to 5111 to avoid this)")
            log.error(f"    System Settings > General > AirDrop & Handoff > AirPlay Receiver → off")
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
