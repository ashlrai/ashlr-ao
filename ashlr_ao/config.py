"""
Ashlr AO — Configuration

DEFAULT_CONFIG, Config dataclass, deep_merge, and load_config.
"""

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ashlr_ao.constants import ASHLR_DIR, log


DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 5111, "log_level": "INFO", "require_auth": False, "auth_token": ""},
    "agents": {
        "max_concurrent": 16,
        "default_role": "general",
        "default_working_dir": os.getcwd(),
        "output_capture_interval_sec": 1.0,
        "memory_limit_mb": 2048,
        "default_backend": "claude-code",
        "backends": {
            "claude-code": {"command": "claude", "args": ["--dangerously-skip-permissions"]},
            "codex": {"command": "codex", "args": []},
        },
    },
    "voice": {"enabled": True, "ptt_key": "Space", "feedback_sounds": True},
    "display": {"theme": "dark", "cards_per_row": 4},
    "llm": {
        "enabled": False,
        "provider": "xai",
        "model": "grok-4-1-fast-reasoning",
        "api_key_env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "summary_interval_sec": 10.0,
        "max_output_lines": 30,
        "meta_interval_sec": 30.0,
    },
    "licensing": {"key": ""},
}


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 5111
    log_level: str = "INFO"
    max_agents: int = 16
    default_role: str = "general"
    default_working_dir: str = "~/Projects"
    output_capture_interval: float = 1.0
    output_max_lines: int = 2000  # Max lines per agent output buffer (deque maxlen)
    memory_limit_mb: int = 2048
    claude_command: str = "claude"
    claude_args: list = field(default_factory=lambda: ["--dangerously-skip-permissions"])
    demo_mode: bool = False
    # Auth
    require_auth: bool = False
    auth_token: str = ""
    # Multi-backend support
    backends: dict = field(default_factory=lambda: {
        "claude-code": {"command": "claude", "args": ["--dangerously-skip-permissions"]},
        "codex": {"command": "codex", "args": []},
    })
    default_backend: str = "claude-code"
    # Voice
    voice_feedback: bool = True
    # Idle agent reaping
    idle_agent_ttl: int = 3600  # seconds before idle/complete agents are reaped
    # LLM summary config
    llm_enabled: bool = False
    llm_provider: str = "xai"
    llm_model: str = "grok-4-1-fast-reasoning"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.x.ai/v1"
    llm_summary_interval: float = 10.0
    llm_max_output_lines: int = 30
    # Configurable alert thresholds
    health_low_threshold: float = 0.3
    health_critical_threshold: float = 0.1
    stall_timeout_minutes: int = 5
    hung_timeout_minutes: int = 10
    # Meta-agent analysis interval (fleet analysis via LLM)
    llm_meta_interval: float = 30.0
    # Cost budget
    cost_budget_usd: float = 0.0  # 0 = no limit
    cost_budget_auto_pause: bool = False
    # Resource exhaustion cascade protection
    system_cpu_pressure_threshold: float = 90.0
    system_memory_pressure_threshold: float = 90.0
    spawn_pressure_block: bool = True
    agent_memory_pause_pct: float = 0.85
    context_auto_pause_threshold: float = 0.95
    pathological_error_window_sec: float = 60.0
    max_pathological_restarts: int = 1
    # Auto-pilot (Wave 3)
    auto_restart_on_stall: bool = True
    auto_approve_enabled: bool = False
    auto_approve_patterns: list = field(default_factory=list)
    auto_pause_on_critical_health: bool = False
    # Coordination (Wave 5) — reserved, enforcement logic not yet implemented
    file_lock_enforcement: bool = False
    # Licensing
    license_key: str = ""
    # Archive rotation
    archive_max_rows_per_agent: int = 50000
    archive_retention_hours: int = 48
    # Output flood protection
    flood_threshold_lines_per_min: int = 3000
    flood_sustained_ticks: int = 3
    # Server stats
    _stats_requests: int = 0
    _stats_start_time: float = 0.0
    # Alert patterns
    alert_patterns: list = field(default_factory=lambda: [
        {"pattern": r"(?i)CRITICAL|FATAL|panic|segfault|segmentation fault", "severity": "critical", "label": "Critical Error"},
        {"pattern": r"(?i)out of memory|OOM|memory exhausted|MemoryError", "severity": "critical", "label": "Memory Issue"},
        {"pattern": r"(?i)permission denied|access denied|unauthorized", "severity": "warning", "label": "Access Error"},
        {"pattern": r"(?i)disk full|no space left|ENOSPC", "severity": "critical", "label": "Disk Full"},
        {"pattern": r"(?i)connection refused|ECONNREFUSED|timeout exceeded", "severity": "warning", "label": "Connection Error"},
    ])

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "max_agents": self.max_agents,
            "default_role": self.default_role,
            "default_working_dir": self.default_working_dir,
            "output_capture_interval": self.output_capture_interval,
            "memory_limit_mb": self.memory_limit_mb,
            "demo_mode": self.demo_mode,
            "default_backend": self.default_backend,
            "backends": {k: {"command": v.get("command", ""), "available": bool(shutil.which(v.get("command", "")))} for k, v in self.backends.items() if isinstance(v, dict)},
            "llm_enabled": self.llm_enabled,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_summary_interval": self.llm_summary_interval,
            "llm_meta_interval": self.llm_meta_interval,
            "voice_feedback": self.voice_feedback,
            "idle_agent_ttl": self.idle_agent_ttl,
            "health_low_threshold": self.health_low_threshold,
            "health_critical_threshold": self.health_critical_threshold,
            "stall_timeout_minutes": self.stall_timeout_minutes,
            "hung_timeout_minutes": self.hung_timeout_minutes,
            "intelligence_enabled": self.llm_enabled,  # backward compat alias
            "cost_budget_usd": self.cost_budget_usd,
            "cost_budget_auto_pause": self.cost_budget_auto_pause,
            "alert_patterns": self.alert_patterns,
            "auto_restart_on_stall": self.auto_restart_on_stall,
            "auto_approve_enabled": self.auto_approve_enabled,
            "auto_approve_patterns": self.auto_approve_patterns,
            "auto_pause_on_critical_health": self.auto_pause_on_critical_health,
        }


def load_config(has_claude: bool = True) -> Config:
    config_dir = ASHLR_DIR
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "ashlr.yaml"

    # Also check for config in the project directory
    local_config = Path(__file__).parent / "ashlr.yaml"

    raw = DEFAULT_CONFIG.copy()

    if config_path.exists():
        try:
            with open(config_path) as f:
                user_config = yaml.safe_load(f) or {}
            raw = deep_merge(raw, user_config)
        except Exception as e:
            log.warning(f"Failed to load config from {config_path}: {e}")
    elif local_config.exists():
        try:
            shutil.copy2(local_config, config_path)
            with open(config_path) as f:
                user_config = yaml.safe_load(f) or {}
            raw = deep_merge(raw, user_config)
            log.info(f"Copied config to {config_path}")
        except Exception as e:
            log.warning(f"Failed to copy config: {e}")
    else:
        try:
            with open(config_path, "w") as f:
                yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
            log.info(f"Created default config at {config_path}")
        except Exception as e:
            log.warning(f"Failed to write default config: {e}")

    server = raw.get("server", {})
    agents = raw.get("agents", {})
    backends = agents.get("backends", {})
    claude_backend = backends.get("claude-code", {})
    voice = raw.get("voice", {})
    llm = raw.get("llm", {})

    default_wd = agents.get("default_working_dir", os.getcwd())
    default_wd = os.path.expanduser(default_wd)
    if not os.path.isdir(default_wd):
        log.warning(f"default_working_dir {default_wd!r} does not exist, using server CWD")
        default_wd = os.getcwd()

    def _validate(value, validator, default, name):
        if validator(value):
            return value
        log.warning(f"Invalid config value for {name}: {value!r}, using default: {default!r}")
        return default

    alerts = raw.get("alerts", {})
    max_agents_val = agents.get("max_concurrent", 16)
    max_agents_val = _validate(max_agents_val, lambda v: isinstance(v, int) and 1 <= v <= 100, 16, "max_concurrent")
    output_interval_val = agents.get("output_capture_interval_sec", 1.0)
    output_interval_val = _validate(output_interval_val, lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 30.0, 1.0, "output_capture_interval_sec")
    memory_limit_val = agents.get("memory_limit_mb", 2048)
    memory_limit_val = _validate(memory_limit_val, lambda v: isinstance(v, int) and 256 <= v <= 32768, 2048, "memory_limit_mb")
    idle_ttl_val = agents.get("idle_agent_ttl", alerts.get("idle_ttl_seconds", 3600))
    idle_ttl_val = _validate(idle_ttl_val, lambda v: isinstance(v, int) and 300 <= v <= 86400, 3600, "idle_agent_ttl")
    llm_interval_val = llm.get("summary_interval_sec", 10.0)
    llm_interval_val = _validate(llm_interval_val, lambda v: isinstance(v, (int, float)) and 3.0 <= v <= 120.0, 10.0, "llm_summary_interval")
    llm_meta_interval_val = llm.get("meta_interval_sec", 30.0)
    llm_meta_interval_val = _validate(llm_meta_interval_val, lambda v: isinstance(v, (int, float)) and 5.0 <= v <= 300.0, 30.0, "llm_meta_interval")
    health_low_val = alerts.get("health_low_threshold", 0.3)
    health_low_val = _validate(health_low_val, lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0, 0.3, "health_low_threshold")
    health_crit_val = alerts.get("health_critical_threshold", 0.1)
    health_crit_val = _validate(health_crit_val, lambda v: isinstance(v, (int, float)) and 0.0 < v <= 1.0, 0.1, "health_critical_threshold")
    stall_val = alerts.get("stall_timeout_minutes", 5)
    stall_val = _validate(stall_val, lambda v: isinstance(v, int) and 1 <= v <= 60, 5, "stall_timeout_minutes")
    hung_val = alerts.get("hung_timeout_minutes", 10)
    hung_val = _validate(hung_val, lambda v: isinstance(v, int) and 1 <= v <= 120, 10, "hung_timeout_minutes")

    yaml_alert_patterns = alerts.get("patterns", [])
    if yaml_alert_patterns and isinstance(yaml_alert_patterns, list):
        validated_alert_patterns = []
        for ap in yaml_alert_patterns:
            if isinstance(ap, dict) and "pattern" in ap:
                try:
                    re.compile(ap["pattern"])
                    validated_alert_patterns.append({
                        "pattern": ap["pattern"],
                        "severity": ap.get("severity", "warning"),
                        "label": ap.get("label", "Alert"),
                    })
                except re.error as e:
                    log.warning(f"Invalid alert pattern regex: {ap.get('pattern')!r} — {e}")
        alert_patterns_final = validated_alert_patterns if validated_alert_patterns else None
    else:
        alert_patterns_final = None

    api_key_env = llm.get("api_key_env", "XAI_API_KEY")
    llm_api_key = os.environ.get(api_key_env, "")

    if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("XAI_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is deprecated for intelligence features. Set XAI_API_KEY instead (xAI Grok via OpenAI-compatible API).")

    require_auth = server.get("require_auth", False)
    if os.environ.get("ASHLR_REQUIRE_AUTH", "").lower() in ("true", "1", "yes"):
        require_auth = True
    auth_token = server.get("auth_token", "")
    if require_auth and not auth_token:
        import secrets as _secrets
        auth_token = _secrets.token_urlsafe(18)
        log.info(f"Auto-generated auth token: {auth_token[:8]}...{auth_token[-4:]}")
        try:
            if config_path.exists():
                with open(config_path) as f:
                    raw_yaml = yaml.safe_load(f) or {}
            else:
                raw_yaml = {}
            raw_yaml.setdefault("server", {})["auth_token"] = auth_token
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix='.yaml.tmp')
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    yaml.dump(raw_yaml, f, default_flow_style=False, sort_keys=False)
                Path(tmp_path).rename(config_path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception as e:
            log.warning(f"Failed to save auto-generated auth token to config: {e}")

    port_raw = os.environ.get("ASHLR_PORT")
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            log.warning(f"Invalid ASHLR_PORT '{port_raw}', using default")
            port = server.get("port", 5111)
    else:
        port = server.get("port", 5111)

    host = os.environ.get("ASHLR_HOST", server.get("host", "127.0.0.1"))

    licensing = raw.get("licensing", {})
    license_key_val = licensing.get("key", "")

    # Auto-pilot settings (Wave 3)
    autopilot = raw.get("autopilot", {})
    auto_approve_patterns_raw = autopilot.get("auto_approve_patterns", [])
    validated_approve_patterns = []
    if isinstance(auto_approve_patterns_raw, list):
        for ap in auto_approve_patterns_raw:
            if isinstance(ap, dict) and "pattern" in ap:
                try:
                    re.compile(ap["pattern"])
                    validated_approve_patterns.append({
                        "pattern": ap["pattern"],
                        "response": ap.get("response", "yes"),
                    })
                except re.error as e:
                    log.warning(f"Invalid auto-approve pattern regex: {ap.get('pattern')!r} — {e}")

    return Config(
        host=host,
        port=port,
        log_level=server.get("log_level", "INFO"),
        max_agents=max_agents_val,
        default_role=agents.get("default_role", "general"),
        default_working_dir=default_wd,
        output_capture_interval=output_interval_val,
        memory_limit_mb=memory_limit_val,
        claude_command=claude_backend.get("command", "claude"),
        claude_args=claude_backend.get("args", ["--dangerously-skip-permissions"]),
        demo_mode=not has_claude,
        voice_feedback=voice.get("feedback_sounds", True),
        require_auth=require_auth,
        auth_token=auth_token,
        backends=backends or DEFAULT_CONFIG["agents"]["backends"],
        default_backend=agents.get("default_backend", "claude-code"),
        llm_enabled=llm.get("enabled", False) and bool(llm_api_key),
        llm_provider=llm.get("provider", "xai"),
        llm_model=llm.get("model", "grok-4-1-fast-reasoning"),
        llm_api_key=llm_api_key,
        llm_base_url=llm.get("base_url", "https://api.x.ai/v1"),
        llm_summary_interval=llm_interval_val,
        llm_max_output_lines=llm.get("max_output_lines", 30),
        llm_meta_interval=llm_meta_interval_val,
        idle_agent_ttl=idle_ttl_val,
        health_low_threshold=health_low_val,
        health_critical_threshold=health_crit_val,
        stall_timeout_minutes=stall_val,
        hung_timeout_minutes=hung_val,
        cost_budget_usd=float(raw.get("cost_budget_usd", 0)),
        cost_budget_auto_pause=bool(raw.get("cost_budget_auto_pause", False)),
        license_key=license_key_val,
        auto_restart_on_stall=bool(autopilot.get("auto_restart_on_stall", True)),
        auto_approve_enabled=bool(autopilot.get("auto_approve_enabled", False)),
        auto_pause_on_critical_health=bool(autopilot.get("auto_pause_on_critical_health", False)),
        **({"auto_approve_patterns": validated_approve_patterns} if validated_approve_patterns else {}),
        **({"alert_patterns": alert_patterns_final} if alert_patterns_final else {}),
    )
