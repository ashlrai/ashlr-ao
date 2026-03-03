"""
Ashlr AO — Database Layer

Async SQLite persistence for agent history, projects, workflows,
users, organizations, sessions, and events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ashlr_ao.constants import ASHLR_DIR
from ashlr_ao.models import Agent, Organization, User

log = logging.getLogger("ashlr")


class Database:
    """Async SQLite layer for agent history, projects, and workflows."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (ASHLR_DIR / "ashlr.db")
        self._db: aiosqlite.Connection | None = None

    async def _safe_commit(self, timeout: float = 3.0) -> bool:
        """Commit with timeout to prevent hangs if DB is locked or unresponsive. Returns True on success."""
        if not self._db:
            return False
        try:
            await asyncio.wait_for(self._db.commit(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.error(f"Database commit timed out after {timeout}s")
            return False
        except Exception as e:
            log.error(f"Database commit failed: {e}")
            return False

    async def init(self) -> None:
        # Close any existing connection (safe for retries)
        if self._db:
            try:
                await self._db.close()
            except Exception as e:
                log.warning(f"Failed to close existing DB connection: {e}")
            self._db = None
        self._db = await aiosqlite.connect(str(self.db_path))
        try:
            self._db.row_factory = aiosqlite.Row
            # SQLite performance PRAGMAs
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA cache_size=-8000")

            await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS agents_history (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                project_id TEXT,
                task TEXT,
                summary TEXT,
                status TEXT,
                working_dir TEXT,
                backend TEXT,
                created_at TEXT,
                completed_at TEXT,
                duration_sec INTEGER,
                context_pct REAL,
                output_preview TEXT
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                agents_json TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_messages (
                id TEXT PRIMARY KEY,
                from_agent_id TEXT NOT NULL,
                to_agent_id TEXT,
                content TEXT NOT NULL,
                created_at TEXT,
                read_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_messages_to ON agent_messages(to_agent_id);
            CREATE INDEX IF NOT EXISTS idx_history_completed ON agents_history(completed_at);
            CREATE INDEX IF NOT EXISTS idx_messages_from ON agent_messages(from_agent_id);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON agent_messages(created_at);

            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                agent_id TEXT,
                agent_name TEXT,
                message TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_activity_agent ON activity_events(agent_id);
            CREATE INDEX IF NOT EXISTS idx_activity_type ON activity_events(event_type);

            CREATE TABLE IF NOT EXISTS file_locks (
                file_path TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_name TEXT,
                locked_at TEXT NOT NULL,
                PRIMARY KEY (file_path, agent_id)
            );

            CREATE TABLE IF NOT EXISTS agent_output_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                line_offset INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_output_archive_agent ON agent_output_archive(agent_id, line_offset);

            CREATE TABLE IF NOT EXISTS agent_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'general',
                backend TEXT DEFAULT 'claude-code',
                task TEXT DEFAULT '',
                system_prompt TEXT DEFAULT '',
                model TEXT DEFAULT '',
                tools_allowed TEXT DEFAULT '',
                working_dir TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scratchpad (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT DEFAULT '',
                set_by TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, key)
            );

            CREATE TABLE IF NOT EXISTS output_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                line_index INTEGER NOT NULL,
                line_text TEXT DEFAULT '',
                annotation TEXT DEFAULT '',
                color TEXT DEFAULT 'accent',
                created_at TEXT NOT NULL
            );
                CREATE INDEX IF NOT EXISTS idx_bookmarks_agent ON output_bookmarks(agent_id);

            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'member',
                org_id TEXT REFERENCES organizations(id),
                created_at TEXT,
                last_login TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                created_at TEXT,
                expires_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            """)
            await self._safe_commit()

            # Safe migrations: add columns if missing
            try:
                await self._db.execute("ALTER TABLE agent_messages ADD COLUMN message_type TEXT DEFAULT 'text'")
                await self._safe_commit()
            except Exception:
                pass  # Column already exists

            # Each ALTER must be its own try/except to handle partial migration states
            for col_sql in [
                "ALTER TABLE agents_history ADD COLUMN tokens_input INTEGER DEFAULT 0",
                "ALTER TABLE agents_history ADD COLUMN tokens_output INTEGER DEFAULT 0",
                "ALTER TABLE agents_history ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0",
                "ALTER TABLE agents_history ADD COLUMN resumable INTEGER DEFAULT 0",
                "ALTER TABLE agents_history ADD COLUMN system_prompt TEXT DEFAULT ''",
                "ALTER TABLE agents_history ADD COLUMN plan_mode INTEGER DEFAULT 0",
                "ALTER TABLE agents_history ADD COLUMN owner_id TEXT DEFAULT ''",
                "ALTER TABLE projects ADD COLUMN org_id TEXT DEFAULT ''",
                "ALTER TABLE agents_history ADD COLUMN model TEXT DEFAULT ''",
                "ALTER TABLE agents_history ADD COLUMN tools_allowed TEXT DEFAULT ''",
                "ALTER TABLE agents_history ADD COLUMN git_branch TEXT DEFAULT ''",
                "ALTER TABLE organizations ADD COLUMN license_key TEXT DEFAULT ''",
                "ALTER TABLE organizations ADD COLUMN plan TEXT DEFAULT 'community'",
            ]:
                try:
                    await self._db.execute(col_sql)
                    await self._safe_commit()
                except Exception:
                    pass  # Column already exists

            # Seed built-in workflows if empty
            async with self._db.execute("SELECT COUNT(*) FROM workflows") as cur:
                row = await cur.fetchone()
                if row[0] == 0:
                    await self._seed_default_workflows()

            log.info(f"Database initialized at {self.db_path}")
        except Exception:
            # Close leaked connection on init failure
            if self._db:
                try:
                    await self._db.close()
                except Exception as e:
                    log.warning(f"Failed to close DB during init error cleanup: {e}")
                self._db = None
            raise

    async def _seed_default_workflows(self) -> None:
        defaults = [
            {
                "id": "builtin-code-review",
                "name": "Code Review",
                "description": "Backend + Security + Reviewer agents for thorough code review",
                "agents_json": json.dumps([
                    {"role": "backend", "task": "Review the codebase for bugs and logic errors"},
                    {"role": "security", "task": "Audit for security vulnerabilities"},
                    {"role": "reviewer", "task": "Review code quality and suggest improvements"},
                ]),
            },
            {
                "id": "builtin-full-stack",
                "name": "Full Stack",
                "description": "Frontend + Backend + Tester for full-stack development",
                "agents_json": json.dumps([
                    {"role": "frontend", "task": "Build the frontend components"},
                    {"role": "backend", "task": "Build the API and backend logic"},
                    {"role": "tester", "task": "Write comprehensive tests"},
                ]),
            },
        ]
        for wf in defaults:
            await self._db.execute(
                "INSERT INTO workflows (id, name, description, agents_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (wf["id"], wf["name"], wf["description"], wf["agents_json"],
                 datetime.now(timezone.utc).isoformat()),
            )
        await self._safe_commit()

    # ── Auth: Organizations ──

    async def create_org(self, name: str, slug: str) -> Organization:
        if not self._db:
            raise RuntimeError("Database not initialized")
        org_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (org_id, name, slug, now),
        )
        await self._safe_commit()
        return Organization(id=org_id, name=name, slug=slug, created_at=now)

    async def get_org(self, org_id: str) -> Organization | None:
        if not self._db:
            return None
        try:
            async with self._db.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return Organization(
                        id=row["id"], name=row["name"], slug=row["slug"], created_at=row["created_at"],
                        license_key=row["license_key"] if "license_key" in row.keys() else "",
                        plan=row["plan"] if "plan" in row.keys() else "community",
                    )
        except Exception as e:
            log.warning(f"Failed to get org {org_id}: {e}")
        return None

    async def update_org_license(self, org_id: str, key: str, plan: str) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                "UPDATE organizations SET license_key = ?, plan = ? WHERE id = ?",
                (key, plan, org_id),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to update org license: {e}")

    async def get_org_license_key(self, org_id: str) -> str:
        if not self._db:
            return ""
        try:
            async with self._db.execute("SELECT license_key FROM organizations WHERE id = ?", (org_id,)) as cur:
                row = await cur.fetchone()
                return row["license_key"] if row and "license_key" in row.keys() else ""
        except Exception as e:
            log.warning(f"Failed to get org license key: {e}")
            return ""

    # ── Auth: Users ──

    async def create_user(self, email: str, display_name: str, password_hash: str, role: str = "member", org_id: str = "") -> User:
        if not self._db:
            raise RuntimeError("Database not initialized")
        user_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO users (id, email, display_name, password_hash, role, org_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, email.lower().strip(), display_name.strip(), password_hash, role, org_id, now),
        )
        await self._safe_commit()
        return User(id=user_id, email=email.lower().strip(), display_name=display_name.strip(),
                    password_hash=password_hash, role=role, org_id=org_id, created_at=now)

    async def get_user_by_email(self, email: str) -> User | None:
        if not self._db:
            return None
        try:
            async with self._db.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)) as cur:
                row = await cur.fetchone()
                if row:
                    return User(id=row["id"], email=row["email"], display_name=row["display_name"],
                                password_hash=row["password_hash"], role=row["role"], org_id=row["org_id"],
                                created_at=row["created_at"], last_login=row["last_login"] or "")
        except Exception as e:
            log.warning(f"Failed to get user by email: {e}")
        return None

    async def get_user_by_id(self, user_id: str) -> User | None:
        if not self._db:
            return None
        try:
            async with self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return User(id=row["id"], email=row["email"], display_name=row["display_name"],
                                password_hash=row["password_hash"], role=row["role"], org_id=row["org_id"],
                                created_at=row["created_at"], last_login=row["last_login"] or "")
        except Exception as e:
            log.warning(f"Failed to get user by id: {e}")
        return None

    async def update_user_login(self, user_id: str) -> None:
        if not self._db:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            await self._db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to update user login: {e}")

    async def get_org_users(self, org_id: str) -> list[User]:
        if not self._db:
            return []
        try:
            async with self._db.execute("SELECT * FROM users WHERE org_id = ? ORDER BY created_at", (org_id,)) as cur:
                rows = await cur.fetchall()
                return [User(id=r["id"], email=r["email"], display_name=r["display_name"],
                             password_hash=r["password_hash"], role=r["role"], org_id=r["org_id"],
                             created_at=r["created_at"], last_login=r["last_login"] or "") for r in rows]
        except Exception as e:
            log.warning(f"Failed to get org users: {e}")
            return []

    async def user_count(self) -> int:
        if not self._db:
            return 0
        try:
            async with self._db.execute("SELECT COUNT(*) FROM users") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception as e:
            log.warning(f"Failed to count users: {e}")
            return 0

    # ── Auth: Sessions ──

    async def create_session(self, user_id: str) -> str:
        if not self._db:
            raise RuntimeError("Database not initialized")
        session_id = uuid.uuid4().hex + uuid.uuid4().hex[:32]  # 64-char hex
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=24)
        await self._db.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, now.isoformat(), expires.isoformat()),
        )
        await self._safe_commit()
        return session_id

    async def get_session(self, session_id: str) -> dict | None:
        if not self._db:
            return None
        try:
            async with self._db.execute(
                "SELECT user_id, expires_at FROM sessions WHERE id = ?", (session_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                expires = datetime.fromisoformat(row["expires_at"])
                if expires < datetime.now(timezone.utc):
                    # Expired — clean up
                    await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                    await self._safe_commit()
                    return None
                return {"user_id": row["user_id"], "expires_at": row["expires_at"]}
        except Exception as e:
            log.warning(f"Failed to validate session: {e}")
            return None

    async def delete_session(self, session_id: str) -> None:
        if not self._db:
            return
        try:
            await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to delete session: {e}")

    async def delete_expired_sessions(self) -> int:
        if not self._db:
            return 0
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with self._db.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (now,)) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            if count > 0:
                await self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
                await self._safe_commit()
            return count
        except Exception as e:
            log.warning(f"Failed to clean expired sessions: {e}")
            return 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None  # Ensure all guards short-circuit after close

    # ── Output Archive ──

    async def archive_output(self, agent_id: str, lines: list[str], start_offset: int) -> None:
        """Archive overflow output lines to SQLite."""
        if not self._db or not lines:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.executemany(
                "INSERT INTO agent_output_archive (agent_id, line_offset, content, created_at) VALUES (?, ?, ?, ?)",
                [(agent_id, start_offset + i, line, now) for i, line in enumerate(lines)],
            )
            await self._safe_commit()
        except Exception as e:
            log.debug(f"Failed to archive output for {agent_id}: {e}")

    async def rotate_archive(self, agent_id: str, max_rows: int = 50000) -> int:
        """Delete oldest archived rows for an agent if over limit. Returns rows deleted."""
        if not self._db or max_rows <= 0:
            return 0
        try:
            async with self._db.execute(
                "SELECT COUNT(*) FROM agent_output_archive WHERE agent_id = ?", (agent_id,)
            ) as cur:
                row = await cur.fetchone()
                total = row[0] if row else 0
            if total <= max_rows:
                return 0
            excess = total - max_rows
            await self._db.execute(
                "DELETE FROM agent_output_archive WHERE agent_id = ? AND id IN "
                "(SELECT id FROM agent_output_archive WHERE agent_id = ? ORDER BY line_offset ASC LIMIT ?)",
                (agent_id, agent_id, excess),
            )
            await self._safe_commit()
            return excess
        except Exception as e:
            log.debug(f"Failed to rotate archive for {agent_id}: {e}")
            return 0

    async def cleanup_old_archives(self, retention_hours: int = 48) -> int:
        """Delete all archived output older than retention_hours. Returns rows deleted."""
        if not self._db or retention_hours <= 0:
            return 0
        try:
            cutoff = f"-{retention_hours} hours"
            async with self._db.execute(
                "SELECT COUNT(*) FROM agent_output_archive WHERE created_at < datetime('now', ?)", (cutoff,)
            ) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0
            if count > 0:
                await self._db.execute(
                    "DELETE FROM agent_output_archive WHERE created_at < datetime('now', ?)", (cutoff,)
                )
                await self._safe_commit()
                log.info(f"Cleaned up {count} old archive rows (>{retention_hours}h)")
            return count
        except Exception as e:
            log.debug(f"Failed to cleanup old archives: {e}")
            return 0

    async def get_archived_output(self, agent_id: str, offset: int = 0, limit: int = 500) -> tuple[list[str], int]:
        """Retrieve archived output lines. Returns (lines, total_count)."""
        if not self._db:
            return [], 0
        try:
            async with self._db.execute(
                "SELECT COUNT(*) FROM agent_output_archive WHERE agent_id = ?", (agent_id,)
            ) as cur:
                row = await cur.fetchone()
                total = row[0] if row else 0
            async with self._db.execute(
                "SELECT content FROM agent_output_archive WHERE agent_id = ? ORDER BY line_offset LIMIT ? OFFSET ?",
                (agent_id, limit, offset),
            ) as cur:
                rows = await cur.fetchall()
                return [r[0] for r in rows], total
        except Exception as e:
            log.debug(f"Failed to get archived output for {agent_id}: {e}")
            return [], 0

    # ── Agent History ──

    async def save_agent(self, agent: Agent) -> None:
        if not self._db:
            return
        completed_at = datetime.now(timezone.utc).isoformat()
        created = agent.created_at or completed_at
        try:
            created_dt = datetime.fromisoformat(created)
            completed_dt = datetime.fromisoformat(completed_at)
            duration = int((completed_dt - created_dt).total_seconds())
        except Exception:
            duration = 0

        output_preview = "\n".join(list(agent.output_lines)[-50:])

        # An agent is resumable if it completed normally or was killed while working
        resumable = 1 if agent.status in ("complete", "working", "planning", "idle") else 0

        try:
            tools_json = json.dumps(agent.tools_allowed) if isinstance(agent.tools_allowed, list) else ""
        except (TypeError, ValueError):
            tools_json = ""
        try:
            await self._db.execute(
                """INSERT OR REPLACE INTO agents_history
                   (id, name, role, project_id, task, summary, status, working_dir,
                    backend, created_at, completed_at, duration_sec, context_pct, output_preview,
                    tokens_input, tokens_output, estimated_cost_usd,
                    resumable, system_prompt, plan_mode, model, tools_allowed, git_branch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (agent.id, agent.name, agent.role, agent.project_id, agent.task,
                 agent.summary, agent.status, agent.working_dir, agent.backend,
                 agent.created_at, completed_at, duration, agent.context_pct, output_preview,
                 agent.tokens_input, agent.tokens_output, agent.estimated_cost_usd,
                 resumable, getattr(agent, 'system_prompt', ''), int(agent.plan_mode),
                 agent.model or "", tools_json, getattr(agent, 'git_branch', '') or ""),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to save agent {agent.id} ({agent.name}) to history: {e}")

    async def get_agent_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        if not self._db:
            return []
        try:
            async with self._db.execute(
                "SELECT * FROM agents_history ORDER BY completed_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.warning(f"Failed to get agent history: {e}")
            return []

    async def get_agent_history_count(self) -> int:
        if not self._db:
            return 0
        try:
            async with self._db.execute("SELECT COUNT(*) FROM agents_history") as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception as e:
            log.warning(f"Failed to get agent history count: {e}")
            return 0

    async def get_agent_history_item(self, agent_id: str) -> dict | None:
        if not self._db:
            return None
        try:
            async with self._db.execute(
                "SELECT * FROM agents_history WHERE id = ?", (agent_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            log.warning(f"Failed to get agent history item {agent_id}: {e}")
            return None

    async def get_resumable_sessions(self, limit: int = 20) -> list[dict]:
        """Get recently completed agents that can be resumed."""
        if not self._db:
            return []
        try:
            async with self._db.execute(
                """SELECT id, name, role, project_id, task, summary, status, working_dir,
                          backend, created_at, completed_at, duration_sec, context_pct,
                          resumable, plan_mode, model, tools_allowed, git_branch
                   FROM agents_history
                   WHERE resumable = 1
                   ORDER BY completed_at DESC LIMIT ?""",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    # Parse tools_allowed from JSON string back to list
                    ta = d.get("tools_allowed", "")
                    if ta and isinstance(ta, str):
                        try:
                            d["tools_allowed"] = json.loads(ta)
                        except (json.JSONDecodeError, TypeError):
                            d["tools_allowed"] = None
                    else:
                        d["tools_allowed"] = None
                    results.append(d)
                return results
        except Exception:
            return []

    async def get_historical_analytics(self) -> dict:
        """Compute historical success rates, cost breakdowns, and performance metrics."""
        empty = {"success_rate": {}, "cost_by_role": {}, "cost_by_backend": {}, "performance_by_role": {}, "error_patterns": {}, "total_historical": 0}
        if not self._db:
            return empty

        try:
            result: dict = {}

            # Success rate by role
            async with self._db.execute(
                """SELECT role,
                          COUNT(*) as total,
                          SUM(CASE WHEN status NOT IN ('error') THEN 1 ELSE 0 END) as successes,
                          AVG(duration_sec) as avg_duration,
                          SUM(estimated_cost_usd) as total_cost
                   FROM agents_history GROUP BY role"""
            ) as cur:
                rows = await cur.fetchall()
                by_role = {}
                for r in rows:
                    role = r["role"] or "unknown"
                    total = r["total"] or 0
                    successes = r["successes"] or 0
                    by_role[role] = {
                        "total": total,
                        "successes": successes,
                        "success_rate": round(successes / total * 100, 1) if total > 0 else 0,
                        "avg_duration_sec": round(r["avg_duration"] or 0),
                        "total_cost_usd": round(r["total_cost"] or 0, 4),
                    }
                result["success_rate_by_role"] = by_role

            # Success rate by backend
            async with self._db.execute(
                """SELECT backend,
                          COUNT(*) as total,
                          SUM(CASE WHEN status NOT IN ('error') THEN 1 ELSE 0 END) as successes,
                          AVG(duration_sec) as avg_duration,
                          SUM(estimated_cost_usd) as total_cost
                   FROM agents_history GROUP BY backend"""
            ) as cur:
                rows = await cur.fetchall()
                by_backend = {}
                for r in rows:
                    backend = r["backend"] or "unknown"
                    total = r["total"] or 0
                    successes = r["successes"] or 0
                    by_backend[backend] = {
                        "total": total,
                        "successes": successes,
                        "success_rate": round(successes / total * 100, 1) if total > 0 else 0,
                        "avg_duration_sec": round(r["avg_duration"] or 0),
                        "total_cost_usd": round(r["total_cost"] or 0, 4),
                    }
                result["success_rate_by_backend"] = by_backend

            # Recent error patterns from activity_events
            async with self._db.execute(
                """SELECT event_type, COUNT(*) as cnt
                   FROM activity_events
                   WHERE event_type LIKE '%error%' OR event_type LIKE '%fail%'
                   GROUP BY event_type ORDER BY cnt DESC LIMIT 10"""
            ) as cur:
                rows = await cur.fetchall()
                result["error_patterns"] = {r["event_type"]: r["cnt"] for r in rows}

            # Total historical
            async with self._db.execute("SELECT COUNT(*) FROM agents_history") as cur:
                row = await cur.fetchone()
                result["total_historical"] = row[0] if row else 0

            # Cost over recent sessions (last 50 agents)
            async with self._db.execute(
                """SELECT role, backend, estimated_cost_usd, duration_sec, status
                   FROM agents_history ORDER BY completed_at DESC LIMIT 50"""
            ) as cur:
                rows = await cur.fetchall()
                recent_cost_by_role: dict[str, float] = {}
                recent_cost_by_backend: dict[str, float] = {}
                for r in rows:
                    role = r["role"] or "unknown"
                    backend = r["backend"] or "unknown"
                    cost = r["estimated_cost_usd"] or 0
                    recent_cost_by_role[role] = recent_cost_by_role.get(role, 0) + cost
                    recent_cost_by_backend[backend] = recent_cost_by_backend.get(backend, 0) + cost
                result["recent_cost_by_role"] = {k: round(v, 4) for k, v in recent_cost_by_role.items()}
                result["recent_cost_by_backend"] = {k: round(v, 4) for k, v in recent_cost_by_backend.items()}

            return result
        except Exception as e:
            log.warning(f"Historical analytics query failed: {e}")
            return empty

    async def find_similar_tasks(self, task_query: str, limit: int = 5) -> list[dict]:
        """Find historical agents with similar tasks using keyword matching."""
        if not self._db or not task_query:
            return []
        # Extract keywords (>3 chars, lowercase)
        keywords = [w.lower() for w in task_query.split() if len(w) > 3]
        if not keywords:
            return []
        # Build OR condition for keyword matching
        conditions = " OR ".join(["LOWER(task) LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        params.append(limit * 3)  # fetch more than needed for scoring
        async with self._db.execute(
            f"""SELECT id, name, role, backend, task, status, duration_sec,
                       estimated_cost_usd, context_pct, completed_at
                FROM agents_history
                WHERE {conditions}
                ORDER BY completed_at DESC LIMIT ?""",
            params,
        ) as cur:
            rows = await cur.fetchall()
        # Score by keyword match count
        scored = []
        for r in rows:
            row_task = (r["task"] or "").lower()
            score = sum(1 for kw in keywords if kw in row_task)
            scored.append((score, dict(r)))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:limit]]

    # ── Projects ──

    async def save_project(self, project: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                """INSERT OR REPLACE INTO projects (id, name, path, description, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project["id"], project["name"], project["path"],
                 project.get("description", ""), project.get("created_at", now), now),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to save project: {e}")

    async def get_projects(self) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute("SELECT * FROM projects ORDER BY name") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def delete_project(self, project_id: str) -> bool:
        if not self._db:
            return False
        async with self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,)) as cur:
            await self._safe_commit()
            return cur.rowcount > 0

    async def get_project(self, project_id: str) -> dict | None:
        """Get a single project by ID."""
        if not self._db:
            return None
        try:
            async with self._db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    async def update_project(self, project_id: str, updates: dict) -> dict | None:
        """Update a project's name, path, and/or description. Returns updated project or None."""
        if not self._db:
            return None
        existing = await self.get_project(project_id)
        if not existing:
            return None
        name = updates.get("name", existing["name"])
        path = updates.get("path", existing["path"])
        description = updates.get("description", existing.get("description", ""))
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                "UPDATE projects SET name = ?, path = ?, description = ?, updated_at = ? WHERE id = ?",
                (name, path, description, now, project_id),
            )
            await self._safe_commit()
            return await self.get_project(project_id)
        except Exception as e:
            log.warning(f"Failed to update project {project_id}: {e}")
            return None

    # ── Workflows ──

    async def save_workflow(self, workflow: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        agents_json = workflow.get("agents_json", "")
        if isinstance(agents_json, list):
            agents_json = json.dumps(agents_json)
        try:
            await self._db.execute(
                """INSERT OR REPLACE INTO workflows (id, name, description, agents_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (workflow["id"], workflow["name"], workflow.get("description", ""),
                 agents_json, workflow.get("created_at", now)),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to save workflow: {e}")

    async def get_workflows(self) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute("SELECT * FROM workflows ORDER BY name") as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["agents"] = json.loads(d.pop("agents_json", "[]"))
                except Exception:
                    d["agents"] = []
                result.append(d)
            return result

    async def get_workflow(self, workflow_id: str) -> dict | None:
        if not self._db:
            return None
        async with self._db.execute(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["agents"] = json.loads(d.pop("agents_json", "[]"))
            except Exception:
                d["agents"] = []
            return d

    async def delete_workflow(self, workflow_id: str) -> bool:
        if not self._db:
            return False
        async with self._db.execute(
            "DELETE FROM workflows WHERE id = ?", (workflow_id,)
        ) as cur:
            await self._safe_commit()
            return cur.rowcount > 0

    # ── Agent Messages ──

    async def save_message(self, msg: dict) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                """INSERT INTO agent_messages (id, from_agent_id, to_agent_id, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (msg["id"], msg["from_agent_id"], msg.get("to_agent_id"),
                 msg["content"], msg["created_at"]),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to save message: {e}")

    async def get_messages_for_agent(self, agent_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute(
            "SELECT * FROM agent_messages WHERE to_agent_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (agent_id, limit, offset),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_messages_between(self, agent_a: str, agent_b: str, limit: int = 50) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute(
            """SELECT * FROM agent_messages
               WHERE (from_agent_id = ? AND to_agent_id = ?)
                  OR (from_agent_id = ? AND to_agent_id = ?)
               ORDER BY created_at DESC LIMIT ?""",
            (agent_a, agent_b, agent_b, agent_a, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_message_count_for_agent(self, agent_id: str) -> int:
        if not self._db:
            return 0
        async with self._db.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE to_agent_id = ?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def mark_messages_read(self, agent_id: str) -> int:
        if not self._db:
            return 0
        try:
            now = datetime.now(timezone.utc).isoformat()
            async with self._db.execute(
                "UPDATE agent_messages SET read_at = ? WHERE to_agent_id = ? AND read_at IS NULL",
                (now, agent_id),
            ) as cur:
                await self._safe_commit()
                return cur.rowcount
        except Exception as e:
            log.warning(f"Failed to mark messages read for {agent_id}: {e}")
            return 0

    async def get_unread_count(self, agent_id: str) -> int:
        if not self._db:
            return 0
        async with self._db.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE to_agent_id = ? AND read_at IS NULL",
            (agent_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # ── Activity Events ──

    async def log_event(
        self,
        event_type: str,
        message: str,
        agent_id: str | None = None,
        agent_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata) if metadata else None
        try:
            await self._db.execute(
                """INSERT INTO activity_events (event_type, agent_id, agent_name, message, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_type, agent_id, agent_name, message, metadata_json, now),
            )
            await self._safe_commit()
        except Exception as e:
            log.debug(f"Failed to log event: {e}")

    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        agent_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        if not self._db:
            return []
        conditions = []
        params: list = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        async with self._db.execute(
            f"SELECT * FROM activity_events {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("metadata_json"):
                    try:
                        d["metadata"] = json.loads(d.pop("metadata_json"))
                    except Exception:
                        d["metadata"] = None
                        d.pop("metadata_json", None)
                else:
                    d.pop("metadata_json", None)
                    d["metadata"] = None
                result.append(d)
            return result

    async def get_events_count(
        self,
        agent_id: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
    ) -> int:
        if self._db is None:
            return 0
        conditions = []
        params: list = []
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with self._db.execute(
            f"SELECT COUNT(*) FROM activity_events {where}", params
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    # ── File Locks ──

    async def set_file_lock(self, file_path: str, agent_id: str, agent_name: str) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                "INSERT OR REPLACE INTO file_locks (file_path, agent_id, agent_name, locked_at) VALUES (?, ?, ?, ?)",
                (file_path, agent_id, agent_name, now),
            )
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to set file lock for {file_path}: {e}")

    async def get_file_locks(self, file_path: str | None = None) -> list[dict]:
        if not self._db:
            return []
        if file_path:
            async with self._db.execute(
                "SELECT * FROM file_locks WHERE file_path = ?", (file_path,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        else:
            async with self._db.execute("SELECT * FROM file_locks") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def release_file_locks(self, agent_id: str) -> None:
        if not self._db:
            return
        try:
            await self._db.execute("DELETE FROM file_locks WHERE agent_id = ?", (agent_id,))
            await self._safe_commit()
        except Exception as e:
            log.warning(f"Failed to release file locks for {agent_id}: {e}")

    # ── Agent Presets ──

    async def get_presets(self) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute("SELECT * FROM agent_presets ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_preset(self, preset_id: str) -> dict | None:
        if not self._db:
            return None
        async with self._db.execute(
            "SELECT * FROM agent_presets WHERE id = ?", (preset_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_preset(self, preset: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_presets
               (id, name, role, backend, task, system_prompt, model, tools_allowed, working_dir, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (preset["id"], preset["name"], preset.get("role", "general"),
             preset.get("backend", "claude-code"), preset.get("task", ""),
             preset.get("system_prompt", ""), preset.get("model", ""),
             preset.get("tools_allowed", ""), preset.get("working_dir", ""),
             preset.get("created_at", now), now),
        )
        await self._safe_commit()

    async def delete_preset(self, preset_id: str) -> bool:
        if not self._db:
            return False
        async with self._db.execute(
            "DELETE FROM agent_presets WHERE id = ?", (preset_id,)
        ) as cur:
            await self._safe_commit()
            return cur.rowcount > 0

    # ── Scratchpad ──

    async def get_scratchpad(self, project_id: str) -> list[dict]:
        if not self._db:
            return []
        async with self._db.execute(
            "SELECT * FROM scratchpad WHERE project_id = ? ORDER BY key", (project_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_scratchpad(self, project_id: str, key: str, value: str, set_by: str = "") -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO scratchpad (project_id, key, value, set_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, key) DO UPDATE SET value=excluded.value, set_by=excluded.set_by, updated_at=excluded.updated_at""",
            (project_id, key, value, set_by, now, now),
        )
        await self._safe_commit()

    async def delete_scratchpad(self, project_id: str, key: str) -> bool:
        if not self._db:
            return False
        async with self._db.execute(
            "DELETE FROM scratchpad WHERE project_id = ? AND key = ?", (project_id, key)
        ) as cur:
            await self._safe_commit()
            return cur.rowcount > 0


    # ── Bookmarks ──

    async def get_bookmarks(self, agent_id: str) -> list[dict]:
        if not self._db:
            return []
        try:
            async with self._db.execute(
                "SELECT * FROM output_bookmarks WHERE agent_id = ? ORDER BY line_index",
                (agent_id,),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except Exception:
            return []

    async def add_bookmark(self, agent_id: str, line_index: int, line_text: str, annotation: str = "", color: str = "accent") -> int:
        if not self._db:
            return -1
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            "INSERT INTO output_bookmarks (agent_id, line_index, line_text, annotation, color, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, line_index, line_text[:500], annotation[:200], color, now),
        ) as cur:
            await self._safe_commit()
            return cur.lastrowid or -1

    async def delete_bookmark(self, bookmark_id: int) -> bool:
        if not self._db:
            return False
        async with self._db.execute(
            "DELETE FROM output_bookmarks WHERE id = ?", (bookmark_id,)
        ) as cur:
            await self._safe_commit()
            return cur.rowcount > 0


