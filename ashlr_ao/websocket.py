"""
Ashlr AO — WebSocket Hub & System Metrics

Real-time WebSocket communication hub for agent updates, events,
and client synchronization. Also collects system-wide metrics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import psutil
from aiohttp import web

from ashlr_ao.extensions import ExtensionScanner
from ashlr_ao.licensing import (
    COMMUNITY_LICENSE,
    License,
    _effective_max_agents,
)
from ashlr_ao.middleware import RateLimiter, _get_client_ip
from ashlr_ao.models import SystemMetrics

if TYPE_CHECKING:
    from ashlr_ao.config import Config
    from ashlr_ao.database import Database
    from ashlr_ao.manager import AgentManager

log = logging.getLogger("ashlr")


# ─────────────────────────────────────────────
# System Metrics
# ─────────────────────────────────────────────

# Initialize CPU percent baseline
psutil.cpu_percent()


async def collect_system_metrics(agent_manager: AgentManager) -> SystemMetrics:
    """Collect system-wide metrics."""
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    active = sum(1 for a in list(agent_manager.agents.values()) if a.status in ("working", "planning"))

    _GiB = 1024 ** 3
    return SystemMetrics(
        cpu_pct=round(cpu, 1),
        cpu_count=psutil.cpu_count() or 1,
        memory_total_gb=round(mem.total / _GiB, 1),
        memory_used_gb=round(mem.used / _GiB, 1),
        memory_available_gb=round(mem.available / _GiB, 1),
        memory_pct=round(mem.percent, 1),
        disk_total_gb=round(disk.total / _GiB, 1),
        disk_used_gb=round(disk.used / _GiB, 1),
        disk_pct=round(disk.percent, 1),
        load_avg=[round(x, 2) for x in os.getloadavg()],
        agents_active=active,
        agents_total=len(agent_manager.agents),
    )


# ─────────────────────────────────────────────
# WebSocket Hub
# ─────────────────────────────────────────────

class WebSocketHub:
    def __init__(self, agent_manager: AgentManager, config: Config, db: Database | None = None):
        self.clients: set[web.WebSocketResponse] = set()
        self.agent_manager = agent_manager
        self.config = config
        self.db = db
        self.max_clients: int = 100
        self._last_sync_time: dict[web.WebSocketResponse, float] = {}
        self.client_meta: dict[web.WebSocketResponse, dict] = {}  # {ws: {user_id, org_id}}
        # Webhook cache (TTL-based to avoid DB query on every event)
        self._webhook_cache: list[dict] | None = None
        self._webhook_cache_time: float = 0.0
        self._webhook_cache_ttl: float = 10.0  # seconds

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=1 * 1024 * 1024)
        await ws.prepare(request)

        # Enforce connection limit — reject if at capacity
        if len(self.clients) >= self.max_clients:
            log.warning(f"WebSocket connection limit reached ({self.max_clients}), rejecting new client")
            await ws.close(code=1013, message=b"Too many connections")
            return ws

        self.clients.add(ws)
        # Store user metadata for org-filtered broadcasting
        user = request.get("user")
        if user:
            self.client_meta[ws] = {"user_id": user.id, "org_id": user.org_id, "role": user.role}
        log.info(f"WebSocket client connected ({len(self.clients)} total)")

        try:
            # Send full state sync
            projects = await self.db.get_projects() if self.db else []
            workflows = await self.db.get_workflows() if self.db else []
            backends_info = {}
            for name, bc in self.agent_manager.backend_configs.items():
                d = bc.to_dict()
                d["name"] = name
                backends_info[name] = d
            try:
                presets = await self.db.get_presets() if self.db else []
            except Exception:
                presets = []
            scanner: ExtensionScanner = request.app.get("extension_scanner")
            extensions_data = scanner.to_dict() if scanner else {"skills": [], "mcp_servers": [], "plugins": [], "scanned_at": ""}
            lic: License = request.app.get("license", COMMUNITY_LICENSE)
            await ws.send_json({
                "type": "sync",
                "agents": [a.to_dict() for a in list(self.agent_manager.agents.values())],
                "projects": projects,
                "workflows": workflows,
                "presets": presets,
                "config": self.config.to_dict(),
                "backends": backends_info,
                "extensions": extensions_data,
                "queue": [t.to_dict() for t in self.agent_manager.task_queue],
                "db_ready": request.app.get("db_ready", False),
                "db_available": request.app.get("db_available", True),
                "license": lic.to_dict(),
                "effective_max_agents": _effective_max_agents(request.app),
            })

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self.handle_message(data, ws)
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                    except Exception as e:
                        log.warning(f"WebSocket message handling error: {e}")
                        try:
                            await ws.send_json({"type": "error", "message": f"Internal error: {e}"})
                        except Exception as e2:
                            log.debug(f"Failed to send WS error response: {e2}")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.error(f"WebSocket error: {str(ws.exception())[:500]}")
                    break
        except Exception as e:
            log.error(f"WebSocket handler error: {e}")
        finally:
            self.clients.discard(ws)
            self._last_sync_time.pop(ws, None)
            self.client_meta.pop(ws, None)
            # Clean up any per-client rate limiter state
            rl: RateLimiter | None = request.app.get("rate_limiter")
            if rl:
                ip = _get_client_ip(request)
                stale_keys = [k for k in rl._buckets if k.startswith(f"{ip}:")]
                for k in stale_keys:
                    del rl._buckets[k]
            log.info(f"WebSocket client disconnected ({len(self.clients)} total)")

        return ws

    def _ws_rate_check(self, ws: web.WebSocketResponse, operation: str) -> bool:
        """Check rate limit for expensive WebSocket operations. Returns True if allowed."""
        app = getattr(self, 'app', None)
        rl: RateLimiter | None = app.get("rate_limiter") if app else None
        if not rl:
            return True
        meta = self.client_meta.get(ws, {})
        key = f"ws:{meta.get('user_id', 'anon')}:{operation}"
        # Spawn: 2/sec burst 5, bulk: 0.5/sec burst 3
        rates = {"spawn": (2.0, 5.0), "bulk-action": (0.5, 3.0)}
        rate, burst = rates.get(operation, (2.0, 10.0))
        allowed, _ = rl.check(key, cost=1.0, rate=rate, burst=burst)
        return allowed

    def _ws_check_ownership(self, ws: web.WebSocketResponse, agent) -> bool:
        """Check if WS client can control this agent. Returns True if allowed."""
        meta = self.client_meta.get(ws, {})
        user_id = meta.get("user_id")
        if not user_id:
            return True  # No auth — allow (require_auth is false)
        user_role = meta.get("role")
        if user_role == "admin":
            return True
        if agent.owner_id and agent.owner_id != user_id:
            return False
        return True

    async def handle_message(self, data: dict, ws: web.WebSocketResponse) -> None:
        msg_type = data.get("type")

        match msg_type:
            case "spawn":
                if not self._ws_rate_check(ws, "spawn"):
                    await ws.send_json({"type": "error", "message": "Rate limit exceeded for spawn"})
                    return
                task = data.get("task", "")
                if not isinstance(task, str) or not task.strip():
                    await ws.send_json({"type": "error", "message": "task is required"})
                    return
                try:
                    agent = await self.agent_manager.spawn(
                        role=data.get("role", self.config.default_role),
                        name=data.get("name"),
                        working_dir=data.get("working_dir"),
                        task=task,
                        plan_mode=data.get("plan_mode", False),
                        backend=data.get("backend", "claude-code"),
                    )
                    if data.get("project_id") and isinstance(data["project_id"], str):
                        agent.project_id = data["project_id"]
                    await self.broadcast({
                        "type": "agent_update",
                        "agent": agent.to_dict(),
                    })
                    await self.broadcast_event("agent_spawned", f"Agent {agent.name} spawned", agent.id, agent.name)
                except ValueError as e:
                    await ws.send_json({"type": "error", "message": str(e)})

            case "send":
                agent_id = data.get("agent_id")
                message = data.get("message", "")
                if agent_id and isinstance(message, str) and message:
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent and not self._ws_check_ownership(ws, agent):
                        await ws.send_json({"type": "error", "message": "Only the agent owner or an admin can send messages"})
                        return
                    if len(message) > 50_000:
                        await ws.send_json({"type": "error", "message": "Message too long (max 50,000 chars)"})
                        return
                    await self.agent_manager.send_message(agent_id, message)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "kill":
                agent_id = data.get("agent_id")
                if agent_id:
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent and not self._ws_check_ownership(ws, agent):
                        await ws.send_json({"type": "error", "message": "Only the agent owner or an admin can perform this action"})
                        return
                    name = agent.name if agent else "unknown"
                    # Archive to history before killing
                    if agent and self.db:
                        try:
                            await self.db.save_agent(agent)
                        except Exception as e:
                            log.warning(f"Failed to archive agent {agent_id}: {e}")
                    success = await self.agent_manager.kill(agent_id)
                    if success:
                        await self.broadcast({
                            "type": "agent_removed",
                            "agent_id": agent_id,
                        })
                        await self.broadcast_event("agent_killed", f"Agent {name} killed", agent_id, name)

            case "pause":
                agent_id = data.get("agent_id")
                if agent_id:
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent and not self._ws_check_ownership(ws, agent):
                        await ws.send_json({"type": "error", "message": "Only the agent owner or an admin can perform this action"})
                        return
                    await self.agent_manager.pause(agent_id)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "resume":
                agent_id = data.get("agent_id")
                message = data.get("message")
                if message is not None and not isinstance(message, str):
                    message = None
                if agent_id:
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent and not self._ws_check_ownership(ws, agent):
                        await ws.send_json({"type": "error", "message": "Only the agent owner or an admin can perform this action"})
                        return
                    await self.agent_manager.resume(agent_id, message)
                    agent = self.agent_manager.agents.get(agent_id)
                    if agent:
                        await self.broadcast({"type": "agent_update", "agent": agent.to_dict()})

            case "agent_message":
                from_id = data.get("from_agent_id")
                to_id = data.get("to_agent_id")
                content = data.get("content", "")
                if from_id and to_id and content and self.db:
                    from_agent = self.agent_manager.agents.get(from_id)
                    if not from_agent:
                        await ws.send_json({"type": "error", "message": f"Source agent {from_id} not found"})
                        return
                    to_agent = self.agent_manager.agents.get(to_id)
                    if not to_agent:
                        await ws.send_json({"type": "error", "message": f"Target agent {to_id} not found"})
                    else:
                        msg = {
                            "id": uuid.uuid4().hex[:8],
                            "from_agent_id": from_id,
                            "to_agent_id": to_id,
                            "content": content,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        await self.db.save_message(msg)
                        to_agent.unread_messages += 1
                        # Also send to agent's tmux session
                        from_agent = self.agent_manager.agents.get(from_id)
                        from_name = from_agent.name if from_agent else from_id
                        sanitized = content.strip()[:500]
                        sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', sanitized)  # Strip control chars except newline/tab
                        await self.agent_manager.send_message(to_id, f"[Message from {from_name}]: {sanitized}")
                        await self.broadcast({"type": "agent_message", "message": msg})
                        await self.broadcast({"type": "agent_update", "agent": to_agent.to_dict()})

            case "sync_request":
                now = time.monotonic()
                last = self._last_sync_time.get(ws, 0.0)
                if now - last < 2.0:
                    await ws.send_json({"type": "error", "message": "sync_request throttled (max 1 per 2s)"})
                    return
                self._last_sync_time[ws] = now
                try:
                    projects = await self.db.get_projects() if self.db else []
                except Exception:
                    projects = []
                try:
                    workflows = await self.db.get_workflows() if self.db else []
                except Exception:
                    workflows = []
                try:
                    presets = await self.db.get_presets() if self.db else []
                except Exception:
                    presets = []
                backends_info = {}
                for name, bc in self.agent_manager.backend_configs.items():
                    d = bc.to_dict()
                    d["name"] = name
                    backends_info[name] = d
                app = getattr(self, 'app', None)
                scanner2: ExtensionScanner | None = app.get("extension_scanner") if app else None
                ext_data = scanner2.to_dict() if scanner2 else {"skills": [], "mcp_servers": [], "plugins": [], "scanned_at": ""}
                lic: License = app.get("license", COMMUNITY_LICENSE) if app else COMMUNITY_LICENSE
                await ws.send_json({
                    "type": "sync",
                    "agents": [a.to_dict() for a in list(self.agent_manager.agents.values())],
                    "projects": projects,
                    "workflows": workflows,
                    "presets": presets,
                    "config": self.config.to_dict(),
                    "backends": backends_info,
                    "extensions": ext_data,
                    "queue": [t.to_dict() for t in self.agent_manager.task_queue],
                    "db_ready": app.get("db_ready", False) if app else False,
                    "db_available": app.get("db_available", True) if app else True,
                    "license": lic.to_dict(),
                    "effective_max_agents": _effective_max_agents(app) if app else 5,
                })

            case _:
                await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    async def broadcast(self, message: dict) -> None:
        if not self.clients:
            return
        # Snapshot to prevent "set changed size during iteration" if clients connect/disconnect mid-broadcast
        clients_snapshot = set(self.clients)
        dead: set[web.WebSocketResponse] = set()

        async def _send(ws: web.WebSocketResponse) -> None:
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=2.0)
            except (ConnectionError, RuntimeError, ConnectionResetError, asyncio.TimeoutError, asyncio.CancelledError):
                dead.add(ws)

        await asyncio.gather(*[_send(ws) for ws in clients_snapshot], return_exceptions=True)
        self.clients -= dead
        for d in dead:
            self.client_meta.pop(d, None)

    async def _get_cached_webhooks(self) -> list[dict]:
        """Get active webhooks with TTL caching to avoid DB query on every event."""
        now = time.monotonic()
        if self._webhook_cache is None or (now - self._webhook_cache_time) > self._webhook_cache_ttl:
            self._webhook_cache = await self.db.get_webhooks(active_only=True) if self.db else []
            self._webhook_cache_time = now
        return self._webhook_cache

    def invalidate_webhook_cache(self) -> None:
        """Call after webhook create/update/delete to force refresh."""
        self._webhook_cache = None

    async def broadcast_event(
        self,
        event: str,
        message: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Broadcast an event to WebSocket clients, persist to DB, and queue for webhooks."""
        from ashlr_ao.constants import redact_secrets
        safe_message = redact_secrets(message)
        payload: dict[str, Any] = {"type": "event", "event": event, "message": safe_message}
        if agent_id:
            payload["agent_id"] = agent_id
        if agent_name:
            payload["agent_name"] = agent_name
        if metadata:
            payload["metadata"] = metadata
        await self.broadcast(payload)
        if self.db:
            await self.db.log_event(event, safe_message, agent_id, agent_name, metadata)
            # Queue for webhook delivery (cached to avoid N+1 DB queries)
            try:
                webhooks = await self._get_cached_webhooks()
                for wh in webhooks:
                    events_filter = wh.get("events", [])
                    if not events_filter or event in events_filter:
                        webhook_payload = {
                            "event": event,
                            "message": safe_message,
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "timestamp": payload.get("timestamp", ""),
                            "metadata": metadata,
                        }
                        await self.db.queue_webhook_delivery(wh["id"], event, webhook_payload)
            except Exception:
                pass  # Don't let webhook queueing break event broadcasting
