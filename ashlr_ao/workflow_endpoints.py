"""
Ashlr AO — Workflow & Fleet Template Endpoints

Route handlers for workflow CRUD, workflow runs, and fleet template management.
Extracted from server.py for modularity.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from ashlr_ao.config import Config
from ashlr_ao.constants import redact_secrets
from ashlr_ao.database import Database
from ashlr_ao.licensing import _check_feature, _effective_max_agents
from ashlr_ao.manager import AgentManager
from ashlr_ao.models import WorkflowRun
from ashlr_ao.middleware import _check_rate
from ashlr_ao.websocket import WebSocketHub

log = logging.getLogger("ashlr")


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
    if r := _check_feature(request, "workflows"):
        return r
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


# ── Fleet template helpers ──


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


# ── Fleet template endpoints ──


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

    if spawned and errors:
        status = 207
    elif spawned:
        status = 201
    else:
        status = 400
    return web.json_response({
        "template": template["name"],
        "spawned": len(spawned),
        "errors": errors,
        "agents": spawned,
    }, status=status)
