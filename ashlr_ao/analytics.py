"""
Ashlr AO — Analytics, Fleet, and Bulk Operation Handlers

REST API handlers for fleet analytics, collaboration graphs, cost tracking,
bulk agent actions, batch spawning, agent suggestions, agent messaging,
spawn validation, and agent search.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from ashlr_ao.auth import _check_agent_ownership
from ashlr_ao.constants import _strip_ansi, redact_secrets
from ashlr_ao.config import Config
from ashlr_ao.database import Database
from ashlr_ao.licensing import (
    COMMUNITY_LICENSE,
    License,
    _check_feature,
    _effective_max_agents,
)
from ashlr_ao.manager import AgentManager
from ashlr_ao.middleware import _check_rate
from ashlr_ao.roles import BUILTIN_ROLES
from ashlr_ao.websocket import WebSocketHub

log = logging.getLogger("ashlr")


# ─────────────────────────────────────────────
# Fleet Analytics
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Collaboration Graph
# ─────────────────────────────────────────────

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
    edge_dict: dict[tuple, dict] = {}  # (from_id, to_id, type) -> edge dict
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


# ─────────────────────────────────────────────
# Cost Tracking
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Bulk Operations
# ─────────────────────────────────────────────

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
        if _check_agent_ownership(request, agent):
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

    if spawned and failed:
        status = 207  # Multi-Status: partial success
    elif spawned:
        status = 201
    else:
        status = 400
    return web.json_response({
        "spawned": spawned,
        "failed": failed,
        "total_spawned": len(spawned),
        "total_failed": len(failed),
    }, status=status)


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

        # Ownership check
        ownership_err = _check_agent_ownership(request, agent)
        if ownership_err:
            failed_items.append({"id": aid, "error": "Permission denied"})
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


# ─────────────────────────────────────────────
# Agent Messaging
# ─────────────────────────────────────────────

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

    try:
        success = await manager.send_message(agent_id, message)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if success:
        # Re-fetch agent after async operation to get latest state
        agent = manager.agents.get(agent_id)
        if agent:
            await hub.broadcast({"type": "agent_update", "agent": agent.to_dict()})
        return web.json_response({"status": "sent"})
    agent_name = agent.name if agent else agent_id
    return web.json_response({"error": f"Failed to send message to '{agent_name}' — agent may have terminated"}, status=500)


# ─────────────────────────────────────────────
# Spawn Validation
# ─────────────────────────────────────────────

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

    # -- Role --
    role = body.get("role", "general")
    if role not in BUILTIN_ROLES:
        errors.append(f"Unknown role '{role}'. Available: {', '.join(BUILTIN_ROLES.keys())}")
    else:
        resolved["role"] = role
        resolved["role_name"] = BUILTIN_ROLES[role].name

    # -- Name --
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

    # -- Backend --
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

    # -- Working directory --
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

    # -- Task --
    task = body.get("task", "")
    if task and len(task) > 10000:
        errors.append("Task description exceeds 10000 character limit")
    elif not task:
        warnings.append("No task provided — agent will start with no instructions")
    resolved["task_length"] = len(task)

    # -- Capacity --
    agent_count = len(manager.agents)
    effective_max = _effective_max_agents(request.app)
    resolved["current_agents"] = agent_count
    resolved["max_agents"] = effective_max
    if agent_count >= effective_max:
        errors.append(f"Maximum agents ({effective_max}) already reached")

    # -- System pressure --
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

    # -- Plan mode --
    plan_mode = body.get("plan_mode", False)
    resolved["plan_mode"] = bool(plan_mode)

    valid = len(errors) == 0
    result: dict[str, object] = {"valid": valid, "resolved": resolved}
    if errors:
        result["errors"] = errors
    if warnings:
        result["warnings"] = warnings
    return web.json_response(result, status=200 if valid else 400)


# ─────────────────────────────────────────────
# Agent Search
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Agent Suggestions
# ─────────────────────────────────────────────

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
