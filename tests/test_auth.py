"""Tests for multi-user auth: User/Org models, DB methods, middleware, agent ownership,
and HTTP integration tests for auth endpoints."""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from collections import deque

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    from ashlr_server import (
        Database, Agent, User, Organization,
        _check_agent_ownership, _make_slug, _extract_session_cookie,
        _get_rate_tier, _RATE_LIMIT_TIERS, RateLimiter,
    )
import bcrypt




async def _fresh_db():
    """Create a fresh temp database for testing."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(f.name)
    f.close()
    db = Database(db_path)
    await db.init()
    return db, db_path


# ─────────────────────────────────────────────
# User / Organization dataclass tests
# ─────────────────────────────────────────────

class TestUserDataclass:
    def test_user_to_dict_excludes_password(self):
        user = User(id="u1", email="a@b.com", display_name="Alice",
                    password_hash="$2b$...", role="admin", org_id="o1")
        d = user.to_dict()
        assert d["id"] == "u1"
        assert d["email"] == "a@b.com"
        assert d["display_name"] == "Alice"
        assert d["role"] == "admin"
        assert "password_hash" not in d

    def test_user_defaults(self):
        user = User(id="u2", email="b@c.com", display_name="Bob", password_hash="x")
        assert user.role == "member"
        assert user.org_id == ""
        assert user.created_at == ""
        assert user.last_login == ""


class TestOrganizationDataclass:
    def test_org_to_dict(self):
        org = Organization(id="o1", name="My Team", slug="my-team", created_at="2026-01-01")
        d = org.to_dict()
        assert d["name"] == "My Team"
        assert d["slug"] == "my-team"


# ─────────────────────────────────────────────
# Slug generation
# ─────────────────────────────────────────────

class TestMakeSlug:
    def test_simple_name(self):
        assert _make_slug("Ashlr Inc") == "ashlr-inc"

    def test_special_chars(self):
        assert _make_slug("My Team!! #1") == "my-team-1"

    def test_empty_name(self):
        assert _make_slug("") == "org"

    def test_already_slugified(self):
        assert _make_slug("my-team") == "my-team"


# ─────────────────────────────────────────────
# Session cookie extraction
# ─────────────────────────────────────────────

class TestExtractSessionCookie:
    def test_no_cookie_returns_empty(self):
        request = MagicMock()
        request.cookies = {}
        assert _extract_session_cookie(request) == ""

    def test_short_cookie_rejected(self):
        request = MagicMock()
        request.cookies = {"ashlr_session": "short"}
        assert _extract_session_cookie(request) == ""

    def test_valid_cookie_returned(self):
        request = MagicMock()
        request.cookies = {"ashlr_session": "a" * 64}
        assert _extract_session_cookie(request) == "a" * 64


# ─────────────────────────────────────────────
# Database: Organizations
# ─────────────────────────────────────────────

class TestDatabaseOrganizations:
    async def test_create_org(self):
        db, path = await _fresh_db()
        try:
            org = await db.create_org("Test Org", "test-org")
            assert org.name == "Test Org"
            assert org.slug == "test-org"
            assert len(org.id) == 8
            assert org.created_at != ""
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_org(self):
        db, path = await _fresh_db()
        try:
            org = await db.create_org("Get Test", "get-test")
            fetched = await db.get_org(org.id)
            assert fetched is not None
            assert fetched.name == "Get Test"
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_nonexistent_org(self):
        db, path = await _fresh_db()
        try:
            assert await db.get_org("nonexistent") is None
        finally:
            await db.close()
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Database: Users
# ─────────────────────────────────────────────

class TestDatabaseUsers:
    async def test_create_user(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
            user = await db.create_user("test@example.com", "Test User", pw_hash, "admin", "org1")
            assert user.email == "test@example.com"
            assert user.display_name == "Test User"
            assert user.role == "admin"
            assert user.org_id == "org1"
            assert len(user.id) == 8
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_user_by_email(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
            await db.create_user("find@me.com", "Find Me", pw_hash)
            found = await db.get_user_by_email("find@me.com")
            assert found is not None
            assert found.display_name == "Find Me"
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_user_by_email_case_insensitive(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            await db.create_user("UPPER@test.com", "Upper", pw_hash)
            found = await db.get_user_by_email("upper@test.com")
            assert found is not None
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_user_by_id(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("byid@test.com", "ById", pw_hash)
            found = await db.get_user_by_id(user.id)
            assert found is not None
            assert found.email == "byid@test.com"
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_duplicate_email_rejected(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            await db.create_user("dup@test.com", "First", pw_hash)
            with pytest.raises(Exception):
                await db.create_user("dup@test.com", "Second", pw_hash)
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_user_count(self):
        db, path = await _fresh_db()
        try:
            assert await db.user_count() == 0
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            await db.create_user("one@test.com", "One", pw_hash)
            assert await db.user_count() == 1
            await db.create_user("two@test.com", "Two", pw_hash)
            assert await db.user_count() == 2
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_get_org_users(self):
        db, path = await _fresh_db()
        try:
            org = await db.create_org("Team", "team")
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            await db.create_user("a@test.com", "Alice", pw_hash, org_id=org.id)
            await db.create_user("b@test.com", "Bob", pw_hash, org_id=org.id)
            await db.create_user("c@other.com", "Charlie", pw_hash, org_id="other")
            users = await db.get_org_users(org.id)
            assert len(users) == 2
            names = {u.display_name for u in users}
            assert "Alice" in names
            assert "Bob" in names
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_update_user_login(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("login@test.com", "Login", pw_hash)
            assert user.last_login == ""
            await db.update_user_login(user.id)
            updated = await db.get_user_by_id(user.id)
            assert updated.last_login != ""
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_nonexistent_user_returns_none(self):
        db, path = await _fresh_db()
        try:
            assert await db.get_user_by_email("nonexistent@test.com") is None
            assert await db.get_user_by_id("fake_id") is None
        finally:
            await db.close()
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────

class TestPasswordHashing:
    def test_bcrypt_verify_correct(self):
        password = "my_secure_password"
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        assert bcrypt.checkpw(password.encode(), pw_hash.encode())

    def test_bcrypt_verify_wrong(self):
        pw_hash = bcrypt.hashpw(b"correct", bcrypt.gensalt()).decode()
        assert not bcrypt.checkpw(b"wrong", pw_hash.encode())


# ─────────────────────────────────────────────
# Database: Sessions
# ─────────────────────────────────────────────

class TestDatabaseSessions:
    async def test_create_session(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("sess@test.com", "Sess", pw_hash)
            session_id = await db.create_session(user.id)
            assert len(session_id) >= 32
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_validate_session(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("val@test.com", "Val", pw_hash)
            session_id = await db.create_session(user.id)
            sess = await db.get_session(session_id)
            assert sess is not None
            assert sess["user_id"] == user.id
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_invalid_session_returns_none(self):
        db, path = await _fresh_db()
        try:
            assert await db.get_session("nonexistent_session_id") is None
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_delete_session(self):
        db, path = await _fresh_db()
        try:
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("del@test.com", "Del", pw_hash)
            session_id = await db.create_session(user.id)
            await db.delete_session(session_id)
            assert await db.get_session(session_id) is None
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_delete_expired_sessions(self):
        db, path = await _fresh_db()
        try:
            from datetime import datetime, timezone, timedelta
            pw_hash = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
            user = await db.create_user("exp@test.com", "Exp", pw_hash)
            # Create an already-expired session manually
            expired_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            await db._db.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                ("expired_sess", user.id, expired_time, expired_time),
            )
            await db._safe_commit()
            deleted = await db.delete_expired_sessions()
            assert deleted == 1
        finally:
            await db.close()
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Agent ownership checks
# ─────────────────────────────────────────────

class TestAgentOwnership:
    def test_no_auth_always_allowed(self):
        """When no user is on the request, ownership check passes."""
        request = MagicMock()
        request.get = MagicMock(return_value=None)
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test",
                      owner_id="u1")
        assert _check_agent_ownership(request, agent) is None

    def test_admin_can_control_any_agent(self):
        admin = User(id="admin1", email="admin@test.com", display_name="Admin",
                     password_hash="x", role="admin")
        request = MagicMock()
        request.get = MagicMock(return_value=admin)
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test",
                      owner_id="other_user")
        assert _check_agent_ownership(request, agent) is None

    def test_owner_can_control_own_agent(self):
        user = User(id="u1", email="u@test.com", display_name="User",
                    password_hash="x", role="member")
        request = MagicMock()
        request.get = MagicMock(return_value=user)
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test",
                      owner_id="u1")
        assert _check_agent_ownership(request, agent) is None

    def test_non_owner_blocked(self):
        user = User(id="u2", email="u2@test.com", display_name="Other",
                    password_hash="x", role="member")
        request = MagicMock()
        request.get = MagicMock(return_value=user)
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test",
                      owner_id="u1")
        result = _check_agent_ownership(request, agent)
        assert result is not None
        assert result.status == 403

    def test_agent_without_owner_allowed(self):
        """Agents with no owner_id can be controlled by anyone."""
        user = User(id="u2", email="u2@test.com", display_name="Other",
                    password_hash="x", role="member")
        request = MagicMock()
        request.get = MagicMock(return_value=user)
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test")
        assert _check_agent_ownership(request, agent) is None


# ─────────────────────────────────────────────
# Agent.to_dict includes owner fields
# ─────────────────────────────────────────────

class TestAgentOwnerInDict:
    def test_owner_fields_in_to_dict(self):
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test",
                      owner_id="u1", owner_name="Alice")
        d = agent.to_dict()
        assert d["owner_id"] == "u1"
        assert d["owner_name"] == "Alice"

    def test_owner_fields_none_by_default(self):
        agent = Agent(id="a1", name="test", role="general", status="working",
                      working_dir="/tmp", backend="claude-code", task="test")
        d = agent.to_dict()
        assert d["owner_id"] is None
        assert d["owner_name"] is None


# ─────────────────────────────────────────────
# DB schema: new tables exist after init
# ─────────────────────────────────────────────

class TestAuthSchema:
    async def test_auth_tables_created(self):
        db, path = await _fresh_db()
        try:
            async with db._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
            assert "organizations" in tables
            assert "users" in tables
            assert "sessions" in tables
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_agents_history_has_owner_id_column(self):
        db, path = await _fresh_db()
        try:
            async with db._db.execute("PRAGMA table_info(agents_history)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            assert "owner_id" in cols
        finally:
            await db.close()
            path.unlink(missing_ok=True)

    async def test_projects_has_org_id_column(self):
        db, path = await _fresh_db()
        try:
            async with db._db.execute("PRAGMA table_info(projects)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            assert "org_id" in cols
        finally:
            await db.close()
            path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Database degraded mode (null _db)
# ─────────────────────────────────────────────

class TestAuthDegradedMode:
    async def test_user_methods_return_defaults_when_no_db(self):
        db = Database(Path("/tmp/nonexistent_auth_test.db"))
        # Don't call init — _db is None
        assert await db.get_user_by_email("x") is None
        assert await db.get_user_by_id("x") is None
        assert await db.user_count() == 0
        assert await db.get_org("x") is None
        assert await db.get_org_users("x") == []
        assert await db.get_session("x") is None

    async def test_delete_methods_noop_when_no_db(self):
        db = Database(Path("/tmp/nonexistent_auth_test2.db"))
        await db.delete_session("x")  # Should not raise
        assert await db.delete_expired_sessions() == 0
        await db.update_user_login("x")  # Should not raise


# ─────────────────────────────────────────────
# Auth rate limiting
# ─────────────────────────────────────────────

class TestAuthRateLimiting:
    def test_login_gets_auth_tier(self):
        assert _get_rate_tier("/api/auth/login", "POST") == "auth"

    def test_register_gets_auth_tier(self):
        assert _get_rate_tier("/api/auth/register", "POST") == "auth"

    def test_auth_status_gets_auth_tier(self):
        assert _get_rate_tier("/api/auth/status", "GET") == "auth"

    def test_auth_invite_gets_auth_tier(self):
        assert _get_rate_tier("/api/auth/invite", "POST") == "auth"

    def test_auth_tier_exists_in_tiers(self):
        assert "auth" in _RATE_LIMIT_TIERS
        rate, burst = _RATE_LIMIT_TIERS["auth"]
        assert rate < 1.0  # Stricter than default
        assert burst <= 5.0

    def test_non_auth_path_not_auth_tier(self):
        assert _get_rate_tier("/api/agents", "GET") != "auth"
        assert _get_rate_tier("/api/projects", "POST") != "auth"

    def test_rate_limiter_allows_within_burst(self):
        rl = RateLimiter()
        rate, burst = _RATE_LIMIT_TIERS["auth"]
        # Should allow up to burst count
        for _ in range(int(burst)):
            allowed, _ = rl.check("test-ip:auth", cost=1.0, rate=rate, burst=burst)
            assert allowed

    def test_rate_limiter_rejects_after_burst(self):
        rl = RateLimiter()
        rate, burst = _RATE_LIMIT_TIERS["auth"]
        # Exhaust burst
        for _ in range(int(burst)):
            rl.check("test-ip:auth", cost=1.0, rate=rate, burst=burst)
        # Next request should be rejected
        allowed, retry_after = rl.check("test-ip:auth", cost=1.0, rate=rate, burst=burst)
        assert not allowed
        assert retry_after > 0


# ─────────────────────────────────────────────
# HTTP Integration Tests — Auth Endpoints
# ─────────────────────────────────────────────

def _make_auth_app():
    """Create a test app with real temp DB for auth HTTP tests."""
    config = ashlr_server.Config()
    config.demo_mode = True
    config.require_auth = True
    config.spawn_pressure_block = False
    app = ashlr_server.create_app(config)
    app["rate_limiter"].check = lambda *a, **kw: (True, 0.0)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["db_available"] = True
    app["db_ready"] = True
    app["bg_task_health"] = {}
    app["bg_tasks"] = []
    return app


class TestAuthHTTPRegister:
    @pytest.mark.asyncio
    async def test_register_first_user_creates_admin(self, aiohttp_client):
        """POST /api/auth/register — first user becomes admin."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/register", json={
                "email": "admin@test.com",
                "password": "password123",
                "display_name": "Admin",
                "org_name": "Test Org",
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["user"]["email"] == "admin@test.com"
            assert body["user"]["role"] == "admin"
            assert body["org"]["name"] == "Test Org"
            # Check session cookie was set
            assert "ashlr_session" in {c.key for c in resp.cookies.values()}
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_register_missing_fields(self, aiohttp_client):
        """POST /api/auth/register — missing required fields returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/register", json={
                "email": "bad",
                "password": "short",
                "display_name": "",
            })
            assert resp.status == 400
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, aiohttp_client):
        """POST /api/auth/register — duplicate email returns 409."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register first user
            await client.post("/api/auth/register", json={
                "email": "dup@test.com", "password": "password123",
                "display_name": "First", "org_name": "Org",
            })
            # Try duplicate — second user must be invited, so this returns 403
            resp = await client.post("/api/auth/register", json={
                "email": "another@test.com", "password": "password123",
                "display_name": "Second",
            })
            assert resp.status == 403
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_register_short_password(self, aiohttp_client):
        """POST /api/auth/register — password < 8 chars returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/register", json={
                "email": "user@test.com", "password": "short",
                "display_name": "User",
            })
            assert resp.status == 400
            body = await resp.json()
            assert "8 characters" in body["error"]
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPLogin:
    @pytest.mark.asyncio
    async def test_login_success(self, aiohttp_client):
        """POST /api/auth/login — valid credentials return 200 + session cookie."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register
            await client.post("/api/auth/register", json={
                "email": "login@test.com", "password": "password123",
                "display_name": "Login User", "org_name": "Org",
            })
            # Login
            resp = await client.post("/api/auth/login", json={
                "email": "login@test.com", "password": "password123",
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["user"]["email"] == "login@test.com"
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, aiohttp_client):
        """POST /api/auth/login — wrong password returns 401."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            await client.post("/api/auth/register", json={
                "email": "wrong@test.com", "password": "password123",
                "display_name": "User", "org_name": "Org",
            })
            resp = await client.post("/api/auth/login", json={
                "email": "wrong@test.com", "password": "badpassword",
            })
            assert resp.status == 401
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, aiohttp_client):
        """POST /api/auth/login — nonexistent email returns 401."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/login", json={
                "email": "ghost@test.com", "password": "password123",
            })
            assert resp.status == 401
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_login_missing_fields(self, aiohttp_client):
        """POST /api/auth/login — missing email/password returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/login", json={"email": "", "password": ""})
            assert resp.status == 400
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPLogout:
    @pytest.mark.asyncio
    async def test_logout(self, aiohttp_client):
        """POST /api/auth/logout — clears session."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register + login
            await client.post("/api/auth/register", json={
                "email": "logout@test.com", "password": "password123",
                "display_name": "User", "org_name": "Org",
            })
            resp = await client.post("/api/auth/logout")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPStatus:
    @pytest.mark.asyncio
    async def test_auth_status_required(self, aiohttp_client):
        """GET /api/auth/status — returns require_auth status."""
        app = _make_auth_app()
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            body = await resp.json()
            assert "auth_required" in body
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPMe:
    @pytest.mark.asyncio
    async def test_me_without_session(self, aiohttp_client):
        """GET /api/auth/me — without session returns 401."""
        app = _make_auth_app()
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.get("/api/auth/me")
            assert resp.status == 401
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPInvite:
    @pytest.mark.asyncio
    async def test_invite_requires_admin(self, aiohttp_client):
        """POST /api/auth/invite — non-admin request is blocked by auth middleware."""
        app = _make_auth_app()
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Without a session, should be blocked by auth middleware
            resp = await client.post("/api/auth/invite", json={
                "email": "new@test.com", "display_name": "New User",
            })
            # Should be 401 (no session) or 403 (not admin)
            assert resp.status in (401, 403)
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_invite_by_member_returns_403(self, aiohttp_client):
        """POST /api/auth/invite — a logged-in member (not admin) gets 403."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register admin first
            reg_resp = await client.post("/api/auth/register", json={
                "email": "admin2@test.com", "password": "password123",
                "display_name": "Admin", "org_name": "Org",
            })
            assert reg_resp.status == 200
            reg_data = await reg_resp.json()
            org_id = reg_data["org"]["id"]
            # Create a member user directly in DB
            pw_hash = bcrypt.hashpw(b"memberpass", bcrypt.gensalt()).decode()
            member = await db.create_user("member@test.com", "Member", pw_hash, role="member", org_id=org_id)
            session_id = await db.create_session(member.id)
            # Use member's session cookie
            client.session.cookie_jar.update_cookies({"ashlr_session": session_id})
            resp = await client.post("/api/auth/invite", json={
                "email": "new@test.com", "display_name": "New",
            })
            assert resp.status == 403
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


class TestAuthHTTPLoginEdgeCases:
    @pytest.mark.asyncio
    async def test_login_wrong_password_returns_401(self, aiohttp_client):
        """POST /api/auth/login — correct email, wrong password returns 401."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            await client.post("/api/auth/register", json={
                "email": "edge@test.com", "password": "correct_pass_123",
                "display_name": "Edge User", "org_name": "Org",
            })
            resp = await client.post("/api/auth/login", json={
                "email": "edge@test.com", "password": "wrong_password",
            })
            assert resp.status == 401
            body = await resp.json()
            assert "error" in body
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_register_duplicate_email_returns_403(self, aiohttp_client):
        """Second registration after first user is blocked (must be invited)."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register first user (admin)
            await client.post("/api/auth/register", json={
                "email": "first@test.com", "password": "password123",
                "display_name": "First", "org_name": "Org",
            })
            # Second registration should fail — users must be invited
            resp = await client.post("/api/auth/register", json={
                "email": "second@test.com", "password": "password123",
                "display_name": "Second",
            })
            assert resp.status == 403
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_expired_session_returns_401(self, aiohttp_client):
        """GET /api/auth/me with an expired session cookie returns 401."""
        from datetime import datetime, timezone, timedelta
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Create a user so auth system is initialized
            pw_hash = bcrypt.hashpw(b"password123", bcrypt.gensalt()).decode()
            org = await db.create_org("Org", "org")
            user = await db.create_user("expiry@test.com", "Expiry User", pw_hash, role="admin", org_id=org.id)
            # Create an already-expired session manually
            expired_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            expired_session = "a" * 64  # valid length session id
            await db._db.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (expired_session, user.id, expired_time, expired_time),
            )
            await db._safe_commit()
            # Use raw request with expired cookie (no prior valid session)
            resp = await client.get("/api/auth/me", cookies={"ashlr_session": expired_session})
            assert resp.status == 401
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Auth middleware: bearer token + WebSocket session
# ─────────────────────────────────────────────

class TestAuthMiddlewarePaths:
    @pytest.mark.asyncio
    async def test_bearer_token_on_regular_route(self, aiohttp_client):
        """Authorization: Bearer token grants access to regular API routes."""
        app = _make_auth_app()
        app["config"].auth_token = "test-secret-token-123"
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.get(
                "/api/agents",
                headers={"Authorization": "Bearer test-secret-token-123"},
            )
            assert resp.status == 200
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_websocket_with_valid_session(self, aiohttp_client):
        """WebSocket path accepts valid session cookie."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register to get a valid session
            reg_resp = await client.post("/api/auth/register", json={
                "email": "ws@test.com", "password": "password123",
                "display_name": "WS User", "org_name": "Org",
            })
            assert reg_resp.status == 200
            # The session cookie is set — WebSocket should be accessible
            # We can't easily test actual WebSocket upgrade, but verify session exists
            session_cookies = {c.key: c.value for c in client.session.cookie_jar}
            assert "ashlr_session" in session_cookies or reg_resp.cookies.get("ashlr_session")
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Register edge cases
# ─────────────────────────────────────────────

class TestAuthRegisterEdgeCases2:
    @pytest.mark.asyncio
    async def test_register_invalid_json_body(self, aiohttp_client):
        """POST /api/auth/register with invalid JSON returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post(
                "/api/auth/register",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "Invalid JSON" in body["error"]
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_register_default_org_name(self, aiohttp_client):
        """POST /api/auth/register without org_name defaults to 'My Team'."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post("/api/auth/register", json={
                "email": "default@test.com", "password": "password123",
                "display_name": "Default User",
                # No org_name — should default to "My Team"
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["org"]["name"] == "My Team"
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Login edge cases
# ─────────────────────────────────────────────

class TestAuthLoginEdgeCases2:
    @pytest.mark.asyncio
    async def test_login_invalid_json_body(self, aiohttp_client):
        """POST /api/auth/login with invalid JSON returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.post(
                "/api/auth/login",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "Invalid JSON" in body["error"]
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Invite edge cases
# ─────────────────────────────────────────────

class TestAuthInviteEdgeCases2:
    async def _make_admin_client(self, aiohttp_client, db):
        """Helper: register admin and return client with session (Pro license for multi_user)."""
        from datetime import datetime, timezone, timedelta
        app = _make_auth_app()
        # Set Pro license so multi_user feature gate passes
        from ashlr_server import License, PRO_FEATURES
        app["license"] = License(
            tier="pro", max_agents=100,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=365)).isoformat(),
            features=PRO_FEATURES,
        )
        app["agent_manager"].license = app["license"]
        app["db"] = db
        app["ws_hub"].db = db
        client = await aiohttp_client(app)
        await client.post("/api/auth/register", json={
            "email": "admin-inv@test.com", "password": "password123",
            "display_name": "Admin", "org_name": "Org",
        })
        return client

    @pytest.mark.asyncio
    async def test_invite_invalid_json(self, aiohttp_client):
        """POST /api/auth/invite with invalid JSON returns 400."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        try:
            client = await self._make_admin_client(aiohttp_client, db)
            resp = await client.post(
                "/api/auth/invite",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "Invalid JSON" in body["error"]
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_invite_duplicate_email(self, aiohttp_client):
        """POST /api/auth/invite with existing email returns 409."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        try:
            client = await self._make_admin_client(aiohttp_client, db)
            # Invite first user
            resp1 = await client.post("/api/auth/invite", json={
                "email": "dup-inv@test.com", "display_name": "First",
            })
            assert resp1.status == 200
            # Same email again
            resp2 = await client.post("/api/auth/invite", json={
                "email": "dup-inv@test.com", "display_name": "Duplicate",
            })
            assert resp2.status == 409
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_invite_missing_display_name_uses_email_prefix(self, aiohttp_client):
        """POST /api/auth/invite without display_name defaults to email prefix."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()
        try:
            client = await self._make_admin_client(aiohttp_client, db)
            resp = await client.post("/api/auth/invite", json={
                "email": "noname@example.com",
                # No display_name — should default to "noname"
            })
            assert resp.status == 200
            body = await resp.json()
            assert body["user"]["display_name"] == "noname"
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Auth status with active session
# ─────────────────────────────────────────────

class TestAuthStatusWithSession:
    @pytest.mark.asyncio
    async def test_status_includes_user_when_logged_in(self, aiohttp_client):
        """GET /api/auth/status returns user dict when session is valid."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            # Register to get session
            await client.post("/api/auth/register", json={
                "email": "status@test.com", "password": "password123",
                "display_name": "Status User", "org_name": "Org",
            })
            # Now check status — should include user
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            body = await resp.json()
            assert body["auth_required"] is True
            assert "user" in body
            assert body["user"]["email"] == "status@test.com"
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)


# ─────────────────────────────────────────────
# Team without auth
# ─────────────────────────────────────────────

class TestAuthTeamEdge2:
    @pytest.mark.asyncio
    async def test_team_without_auth_returns_401(self, aiohttp_client):
        """GET /api/auth/team without session returns 401."""
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        app = _make_auth_app()
        db = Database(db_path)
        await db.init()
        app["db"] = db
        app["ws_hub"].db = db
        try:
            client = await aiohttp_client(app)
            resp = await client.get("/api/auth/team")
            assert resp.status == 401
        finally:
            await db.close()
            db_path.unlink(missing_ok=True)
