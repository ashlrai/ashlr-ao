"""
Ashlr AO — System, Config & License Endpoints

REST API handlers for system metrics, health checks, diagnostics,
server stats, config management, and license activation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import yaml
from aiohttp import web

from ashlr_ao.config import Config, DEFAULT_CONFIG, deep_merge, load_config
from ashlr_ao.constants import ASHLR_DIR, redact_secrets
from ashlr_ao.database import Database
from ashlr_ao.intelligence import IntelligenceClient
from ashlr_ao.licensing import (
    COMMUNITY_LICENSE,
    License,
    PRO_FEATURES,
    _effective_max_agents,
    validate_license,
)
from ashlr_ao.manager import AgentManager
from ashlr_ao.middleware import _check_rate
from ashlr_ao.roles import BUILTIN_ROLES
from ashlr_ao.websocket import WebSocketHub, collect_system_metrics

log = logging.getLogger("ashlr")


# Concurrency guard for config file writes
_config_write_lock = asyncio.Lock()


# ─────────────────────────────────────────────
# System Metrics
# ─────────────────────────────────────────────

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


async def system_metrics_detail(request: web.Request) -> web.Response:
    """GET /api/system/detail — detailed CPU, memory, disk, and top processes."""
    import psutil as _ps

    def _gather() -> dict:
        _GiB = 1024**3
        _MiB = 1024**2
        # Per-CPU usage
        per_cpu = _ps.cpu_percent(interval=None, percpu=True)
        freq = _ps.cpu_freq()
        # Memory breakdown
        mem = _ps.virtual_memory()
        swap = _ps.swap_memory()
        # Disk
        disk = _ps.disk_usage("/")
        # Top processes by CPU
        procs = []
        for p in _ps.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
            try:
                info = p.info
                if info["cpu_percent"] and info["cpu_percent"] > 0:
                    procs.append({
                        "pid": info["pid"],
                        "name": info["name"][:40],
                        "cpu": round(info["cpu_percent"], 1),
                        "mem_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / _MiB, 1),
                        "status": info["status"],
                    })
            except (_ps.NoSuchProcess, _ps.AccessDenied):
                continue
        procs.sort(key=lambda x: x["cpu"], reverse=True)
        return {
            "cpu": {
                "total_pct": round(_ps.cpu_percent(interval=None), 1),
                "per_cpu": [round(c, 1) for c in per_cpu],
                "count_logical": _ps.cpu_count(logical=True) or 1,
                "count_physical": _ps.cpu_count(logical=False) or 1,
                "freq_mhz": round(freq.current) if freq else None,
                "freq_max_mhz": round(freq.max) if freq and freq.max else None,
            },
            "memory": {
                "total_gb": round(mem.total / _GiB, 2),
                "used_gb": round(mem.used / _GiB, 2),
                "available_gb": round(mem.available / _GiB, 2),
                "pct": round(mem.percent, 1),
                "cached_gb": round(getattr(mem, "cached", 0) / _GiB, 2),
                "swap_total_gb": round(swap.total / _GiB, 2),
                "swap_used_gb": round(swap.used / _GiB, 2),
                "swap_pct": round(swap.percent, 1),
            },
            "disk": {
                "total_gb": round(disk.total / _GiB, 1),
                "used_gb": round(disk.used / _GiB, 1),
                "free_gb": round(disk.free / _GiB, 1),
                "pct": round(disk.percent, 1),
            },
            "load_avg": [round(x, 2) for x in os.getloadavg()],
            "top_processes": procs[:15],
        }

    data = await asyncio.to_thread(_gather)
    return web.json_response(data)


# ─────────────────────────────────────────────
# Health Checks
# ─────────────────────────────────────────────

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
    except Exception as e:
        log.debug(f"Could not read server memory: {e}")
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


# ─────────────────────────────────────────────
# Diagnostics & Stats
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# Config Endpoints
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# API Key Endpoints
# ─────────────────────────────────────────────

async def set_api_key(request: web.Request) -> web.Response:
    """POST /api/config/api-key — set the AI API key at runtime."""
    if r := _check_rate(request, cost=10, rate=0.1, burst=2):
        return r

    config: Config = request.app["config"]

    # Admin-only when auth is enabled
    if config.require_auth:
        user = request.get("user")
        if not user or user.role != "admin":
            return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    key = data.get("key", "")
    if not isinstance(key, str) or not key.strip():
        return web.json_response({"error": "API key must be a non-empty string"}, status=400)

    key = key.strip()

    # Set the environment variable so the rest of the system picks it up
    os.environ["XAI_API_KEY"] = key

    # Update config in memory
    config.llm_api_key = key
    config.llm_enabled = True

    # Close old intelligence client session if present
    old_client = request.app.get("intelligence")
    if old_client:
        try:
            await old_client.close()
        except Exception:
            pass

    # Re-initialize intelligence client with updated config
    new_client = IntelligenceClient(config)
    request.app["intelligence"] = new_client

    log.info(f"API key set at runtime, intelligence available={new_client.available}")

    return web.json_response({"ok": True, "available": new_client.available})


async def get_api_key_status(request: web.Request) -> web.Response:
    """GET /api/config/api-key/status — check whether an API key is configured."""
    if r := _check_rate(request, cost=1):
        return r

    config: Config = request.app["config"]
    client = request.app.get("intelligence")

    return web.json_response({
        "has_key": bool(config.llm_api_key),
        "available": bool(client and client.available),
        "model": config.llm_model,
    })


# ─────────────────────────────────────────────
# License Endpoints
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
