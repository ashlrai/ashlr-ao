# Changelog

## 1.6.1 — 2026-03-03

- **Housekeeping**: Removed stale pre-development docs (ARCHITECTURE.md, PHASE1_SPEC.md, REFERENCE_PATTERNS.md)
- **PEP 561**: Added `py.typed` marker for typed package support
- **README**: Added dashboard screenshot
- **Tests**: 23 new tests — `test_constants.py` (15: logging, banner, dependency checks), `test_auth.py` additions (10: bearer token, invalid JSON, invite edge cases, auth status with session)
- 1582 tests across 24 test files

## 1.6.0 — 2026-03-03

### Multi-Repo Workflow (Waves 1-5)

- **Wave 1 — Project foundation**: Project dataclass with git auto-detection, per-project defaults (backend, model, role), DB schema migrations, project-aware agent spawning, quick-switch (`Cmd+1-9`), project dashboard header
- **Wave 2 — Monitoring & focus**: Focus mode (`Cmd+Shift+F`), saved filter views, enhanced global search (`Cmd+/`), filter persistence, agent grouping by project
- **Wave 3 — Auto-pilot**: Auto-restart on stall, auto-approve with hardcoded blocklist (rm -rf, force push, DROP TABLE, etc.), rate-limited 5/min/agent, browser notifications with per-event preferences, health-based auto-pause
- **Wave 4 — Fleet templates**: DB-backed fleet templates with CRUD API, parameterized task variables (`{branch}`, `{project_name}`, `{date}`), one-click deploy to project
- **Wave 5 — Cross-agent intelligence**: Scratchpad WebSocket broadcast, auto-handoff (agent spawns successor with `{prev_summary}` context), project events feed, `file_lock_enforcement` config

### Audit & Hardening

- Full codebase audit (quality, security, coverage) — no critical bugs found
- Security: tool name validation, `system_prompt_extra` 5K cap, path `os.sep` boundary check, symlink traversal fix, XSS fixes
- Robustness: defensive agent deletion, auth fetch error handling, license log clarity
- 203 new tests across 6 new test files and existing file updates

### Stats

- 1559 tests across 23 test files
- 16 Python modules (~13K lines) + dashboard (~21K lines)
- Installable via `pip install ashlr-ao`

## 1.5.2 — 2026-03-03

- **Security**: Tool name validation (alphanumeric/hyphen/underscore only), `system_prompt_extra` capped at 5000 chars, project path boundary check hardened with `os.sep` (prevents prefix-match attacks)
- **Tests**: 19 new tests — spawn input validation (4), project path validation (3), `verify_auth` endpoint (4), `_set_session_cookie` security flags (3), `acknowledge_insight` (2), `agent_suggestions` (3)
- **Infrastructure**: `_set_session_cookie` added to backward-compat shim exports
- 1356 tests across 19 test files (70.6% coverage)

## 1.5.1 — 2026-03-03

- **Security**: Symlink path traversal fix in working_dir validation (`os.path.abspath` → `os.path.realpath`), XSS fix in intelligence insight dismiss buttons (inline `onclick` → event delegation)
- **Robustness**: Defensive agent deletion (`dict.pop` instead of `del`), auth fetch calls use `apiFetch()` for timeout/error handling, license validation log clarity (distinguish empty key from invalid key), removed duplicate import in backward-compat shim
- **Tests**: 18 new tests — `load_config` YAML loading/validation (9), `_supervised_task` crash recovery (3), `meta_agent_loop` edge cases (2), auth edge cases (4)
- **CI**: Coverage threshold bumped from 60% to 65%
- 1337 tests across 19 test files

## 1.5.0 — 2026-03-03

- **Server modularization**: Split 10.8K-line `server.py` into 16 focused modules (`models.py`, `config.py`, `database.py`, `manager.py`, `websocket.py`, `background.py`, `middleware.py`, `intelligence.py`, `auth.py`, `handlers/`, etc.) — `server.py` now ~3.9K lines as re-export hub
- **Security fixes**: WebSocket ownership bypass, config import RCE allowlist, dashboard XSS via innerHTML, clone agent license bypass, bearer token timing attack (hmac.compare_digest), missing ownership check on inter-agent messages
- **Bug fixes**: Agent `cpu_pct` serialization, `_safe_eval_condition` operator detection, path traversal boundary check, WebSocket sync_request missing fields, `_safe_commit` in background tasks, dashboard card double-handler, undefined CSS variable, native `confirm()` replaced with custom modal, dead code cleanup
- **Performance**: bcrypt moved to thread pool, global search to thread pool, synchronous file writes to thread pool, collaboration graph O(N²) → O(1) edge updates, dashboard visibility-change pause for intervals
- **Infrastructure**: Replaced abandoned `aiohttp-cors` with native middleware, `.dockerignore` added, `asyncio_mode = "auto"` in pytest config, `bcrypt` and `PyJWT[crypto]` in requirements.txt
- **Test improvements**: Split `test_lifecycle.py` (2322 → 1552 lines), created `test_intelligence.py`, new tests for health_check_loop, metrics_loop, memory_watchdog_loop, archive_cleanup_loop, IntelligenceClient HTTP interaction
- **Backward compatibility**: `ashlr_server.py` shim fully preserved — all existing imports and test patches continue to work
- 1319 tests across 19 test files

## 1.4.0 — 2026-03-03

- Open-core monetization: Community (free, 5 agents) and Pro (paid, 100 agents) tiers
- Ed25519-signed JWT licensing — fully offline, no phone-home
- License API: `GET/POST/DELETE /api/license/{status,activate,deactivate}`
- Feature gating: intelligence, workflows, fleet presets, multi-user behind Pro
- Dashboard: plan badge in header, license settings section, upgrade prompts on 403
- Admin-only config updates when auth is enabled, max_agents clamped to license ceiling
- `generate_license.py` standalone tool for keypair generation and license signing
- Added PyJWT[crypto] runtime dependency, pytest-aiohttp dev dependency
- ~40 new licensing tests in `tests/test_licensing.py`

## 1.3.1 — 2026-03-03

- Docker: run as non-root user, pin Claude CLI to major version
- Docker Compose: enable auth and set CORS origin by default
- Caddyfile: add HSTS header
- Server: support `ASHLR_REQUIRE_AUTH` env var to force auth on
- Server: truncate auto-generated auth token in logs
- Server: disable CORS credentials when origin is wildcard
- CI: bump coverage threshold to 60%, add pip cache
- Fix GitHub clone URL in README

## 1.3.0 — 2026-03-03

- Production hardening: security headers, ownership checks, rate limiting
- ARIA accessibility improvements across dashboard
- Silent exception logging (16 handlers)
- Archive cleanup background task (48hr retention)
- Enhanced request logging (user_id, body_size, ms)
- Config validators (log_level, host, alert_patterns)
- 1195 tests

## 1.2.0 — 2026-03-02

- Session resume UI in spawn dialog
- Git branch tracking per agent with dashboard badge and filter
- Project UPDATE endpoint (`PUT /api/projects/{id}`)
- DB migrations for model, tools_allowed, git_branch columns
- 1106 tests

## 1.1.1 — 2026-03-02

- Exception logging and auth rate limits
- CI linting and coverage checks

## 1.1.0 — 2026-03-02

- Rename Ashlar to Ashlr with migration support
- CLI args: `--port`, `--host`, `--demo`, `--log-level`, `--version`
- GitHub Actions CI (Python 3.11-3.13) and PyPI publish workflow
- 987 tests

## 1.0.0 — 2026-03-01

- Initial release as pip-installable package (`ashlr-ao`)
- Agent orchestration via tmux with real-time dashboard
- REST + WebSocket APIs, SQLite persistence
- Multi-user auth with session cookies and org model
- Intelligence layer via xAI Grok (summaries, NLU, fleet analysis)
- Docker + Caddy deployment with auto-HTTPS
