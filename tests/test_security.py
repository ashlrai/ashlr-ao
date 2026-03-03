"""Security tests for Ashlr AO — production hardening validation.

Tests cover ownership enforcement, security headers, request size limits,
rate limiter internals, config validation, and WebSocket rate checking.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

TEST_WORKING_DIR = str(Path.home())

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server

from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app


def _make_agent(agent_id="test-id", owner_id="user1", status="working"):
    """Create a test Agent with minimal required fields."""
    return ashlr_server.Agent(
        id=agent_id,
        name="test-agent",
        role="general",
        status=status,
        working_dir="/tmp",
        backend="claude-code",
        task="test task",
        owner_id=owner_id,
    )


def _make_user(user_id="user2", role="member"):
    """Create a test User."""
    return ashlr_server.User(
        id=user_id,
        email=f"{user_id}@test.com",
        display_name=f"User {user_id}",
        password_hash="x",
        role=role,
    )


async def _make_request(app, method="GET", path="/", json_body=None, user=None, match_info=None):
    """Create a mock aiohttp request for direct handler testing."""
    request = MagicMock()
    request.app = app
    request.method = method
    request.path = path
    request.match_info = match_info or {}
    request.get = lambda key, default=None: user if key == "user" else default
    if json_body is not None:
        request.json = AsyncMock(return_value=json_body)
    else:
        request.json = AsyncMock(side_effect=json.JSONDecodeError("", "", 0))
    request.content_length = 0
    request.transport = MagicMock()
    request.transport.get_extra_info = MagicMock(return_value=("127.0.0.1", 12345))
    return request


# ── Ownership Enforcement: Handoff ──


class TestHandoffOwnership:
    @pytest.mark.asyncio
    async def test_handoff_non_owner_blocked(self):
        """POST /api/agents/{id}/handoff by non-owner member returns 403."""
        app = _make_test_app()
        agent = _make_agent(agent_id="src-agent", owner_id="user1")
        target = _make_agent(agent_id="tgt-agent", owner_id="user1")
        app["agent_manager"].agents["src-agent"] = agent
        app["agent_manager"].agents["tgt-agent"] = target

        non_owner = _make_user(user_id="user2", role="member")
        request = await _make_request(
            app,
            method="POST",
            path="/api/agents/src-agent/handoff",
            json_body={"to_agent_id": "tgt-agent"},
            user=non_owner,
            match_info={"id": "src-agent"},
        )

        response = await ashlr_server.handoff_agent(request)
        assert response.status == 403
        body = json.loads(response.body)
        assert "owner" in body["error"].lower() or "admin" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_handoff_admin_allowed(self):
        """POST /api/agents/{id}/handoff by admin user is not blocked by ownership check."""
        app = _make_test_app()
        agent = _make_agent(agent_id="src-agent", owner_id="user1")
        target = _make_agent(agent_id="tgt-agent", owner_id="user1")
        app["agent_manager"].agents["src-agent"] = agent
        app["agent_manager"].agents["tgt-agent"] = target

        admin = _make_user(user_id="admin1", role="admin")
        request = await _make_request(
            app,
            method="POST",
            path="/api/agents/src-agent/handoff",
            json_body={"to_agent_id": "tgt-agent"},
            user=admin,
            match_info={"id": "src-agent"},
        )

        # Mock send_message so tmux send doesn't fail
        app["agent_manager"].send_message = AsyncMock()

        response = await ashlr_server.handoff_agent(request)
        # Admin should NOT get a 403. Expect success (200) since both agents exist.
        assert response.status != 403
        assert response.status == 200
        body = json.loads(response.body)
        assert body["status"] == "handed_off"


# ── Ownership Enforcement: Snapshot ──


class TestSnapshotOwnership:
    @pytest.mark.asyncio
    async def test_snapshot_non_owner_blocked(self):
        """POST /api/agents/{id}/snapshots by non-owner member returns 403."""
        app = _make_test_app()
        agent = _make_agent(agent_id="snap-agent", owner_id="user1")
        app["agent_manager"].agents["snap-agent"] = agent

        non_owner = _make_user(user_id="user2", role="member")
        request = await _make_request(
            app,
            method="POST",
            path="/api/agents/snap-agent/snapshots",
            user=non_owner,
            match_info={"id": "snap-agent"},
        )

        response = await ashlr_server.create_agent_snapshot(request)
        assert response.status == 403
        body = json.loads(response.body)
        assert "owner" in body["error"].lower() or "admin" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_snapshot_admin_allowed(self):
        """POST /api/agents/{id}/snapshots by admin user succeeds."""
        app = _make_test_app()
        agent = _make_agent(agent_id="snap-agent", owner_id="user1")
        app["agent_manager"].agents["snap-agent"] = agent

        admin = _make_user(user_id="admin1", role="admin")
        request = await _make_request(
            app,
            method="POST",
            path="/api/agents/snap-agent/snapshots",
            user=admin,
            match_info={"id": "snap-agent"},
        )

        response = await ashlr_server.create_agent_snapshot(request)
        assert response.status == 201
        body = json.loads(response.body)
        assert body["trigger"] == "manual"
        assert body["agent_id"] == "snap-agent"

    @pytest.mark.asyncio
    async def test_snapshot_no_auth_allowed(self):
        """POST /api/agents/{id}/snapshots with no user on request succeeds (no auth mode)."""
        app = _make_test_app()
        agent = _make_agent(agent_id="snap-agent", owner_id="user1")
        app["agent_manager"].agents["snap-agent"] = agent

        # No user — simulates auth disabled (require_auth=false)
        request = await _make_request(
            app,
            method="POST",
            path="/api/agents/snap-agent/snapshots",
            user=None,
            match_info={"id": "snap-agent"},
        )

        response = await ashlr_server.create_agent_snapshot(request)
        assert response.status == 201
        body = json.loads(response.body)
        assert body["trigger"] == "manual"


# ── Ownership Enforcement: Patch ──


class TestPatchOwnership:
    @pytest.mark.asyncio
    async def test_patch_non_owner_blocked(self):
        """PATCH /api/agents/{id} by non-owner member returns 403."""
        app = _make_test_app()
        agent = _make_agent(agent_id="patch-agent", owner_id="user1")
        app["agent_manager"].agents["patch-agent"] = agent

        non_owner = _make_user(user_id="user2", role="member")
        request = await _make_request(
            app,
            method="PATCH",
            path="/api/agents/patch-agent",
            json_body={"name": "new-name"},
            user=non_owner,
            match_info={"id": "patch-agent"},
        )

        response = await ashlr_server.patch_agent(request)
        assert response.status == 403
        body = json.loads(response.body)
        assert "owner" in body["error"].lower() or "admin" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_patch_owner_allowed(self):
        """PATCH /api/agents/{id} by owner succeeds."""
        app = _make_test_app()
        agent = _make_agent(agent_id="patch-agent", owner_id="user1")
        app["agent_manager"].agents["patch-agent"] = agent

        owner = _make_user(user_id="user1", role="member")
        request = await _make_request(
            app,
            method="PATCH",
            path="/api/agents/patch-agent",
            json_body={"name": "updated-name"},
            user=owner,
            match_info={"id": "patch-agent"},
        )

        response = await ashlr_server.patch_agent(request)
        assert response.status == 200
        body = json.loads(response.body)
        assert body["name"] == "updated-name"


# ── Security Headers ──


class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_csp_header_present(self, aiohttp_client):
        """GET /api/health response includes Content-Security-Policy header."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        csp = resp.headers.get("Content-Security-Policy")
        assert csp is not None
        assert "default-src" in csp
        assert "frame-ancestors 'none'" in csp

    @pytest.mark.asyncio
    async def test_xcto_header_present(self, aiohttp_client):
        """GET /api/health response includes X-Content-Type-Options: nosniff."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_xfo_header_present(self, aiohttp_client):
        """GET /api/health response includes X-Frame-Options: DENY."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        assert resp.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_referrer_policy_header(self, aiohttp_client):
        """GET /api/health response includes Referrer-Policy header."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.get("/api/health")
        assert resp.status == 200
        rp = resp.headers.get("Referrer-Policy")
        assert rp is not None
        assert "strict-origin" in rp


# ── Request Size Limit ──


class TestRequestSizeLimit:
    @pytest.mark.asyncio
    async def test_request_size_limit(self, aiohttp_client):
        """App should enforce a client_max_size of 10MB."""
        app = _make_test_app()
        # Check the app's configured max size (10 * 1024 * 1024 = 10485760)
        assert app._client_max_size == 10 * 1024 * 1024


# ── Rate Limiter Unit Tests ──


class TestRateLimiter:
    def test_rate_limiter_eviction_removes_stale(self):
        """Stale buckets (older than 1 hour) are evicted after 100 checks."""
        rl = ashlr_server.RateLimiter()
        # Manually insert an old bucket
        rl._buckets["old-ip:default"] = {
            "tokens": 10.0,
            "last_refill": time.monotonic() - 7200,  # 2 hours ago
        }
        assert "old-ip:default" in rl._buckets

        # Perform 100 checks to trigger eviction
        for i in range(100):
            rl.check(f"evict-test-{i}:default")

        # The old bucket should have been evicted
        assert "old-ip:default" not in rl._buckets

    def test_rate_limiter_check_count_increments(self):
        """Each call to check() increments the internal _check_count."""
        rl = ashlr_server.RateLimiter()
        assert rl._check_count == 0

        rl.check("ip1:default")
        assert rl._check_count == 1

        rl.check("ip2:default")
        assert rl._check_count == 2

        for _ in range(10):
            rl.check("ip3:default")
        assert rl._check_count == 12

    def test_rate_limiter_eviction_keeps_recent(self):
        """Recent buckets survive eviction triggered at 100 checks."""
        rl = ashlr_server.RateLimiter()
        # Insert a recent bucket
        rl._buckets["fresh-ip:default"] = {
            "tokens": 10.0,
            "last_refill": time.monotonic() - 5,  # 5 seconds ago
        }

        # Perform 100 checks to trigger eviction
        for i in range(100):
            rl.check(f"keep-test-{i}:default")

        # The fresh bucket should still exist
        assert "fresh-ip:default" in rl._buckets

    def test_rate_limiter_blocks_when_exhausted(self):
        """Rate limiter denies requests when tokens are exhausted."""
        rl = ashlr_server.RateLimiter()
        # Use very low burst to quickly exhaust
        for _ in range(3):
            allowed, _ = rl.check("exhaust-ip:spawn", cost=1.0, rate=0.1, burst=3.0)

        # Next check should be blocked
        allowed, retry_after = rl.check("exhaust-ip:spawn", cost=1.0, rate=0.1, burst=3.0)
        assert not allowed
        assert retry_after > 0


# ── Config Validation via PUT /api/config ──


class TestConfigValidation:
    @pytest.mark.asyncio
    async def test_config_rejects_invalid_working_dir(self, aiohttp_client):
        """PUT /api/config with nonexistent working dir returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put(
            "/api/config",
            json={"default_working_dir": "/nonexistent/path/that/does/not/exist/anywhere"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "invalid" in body["error"].lower() or "default_working_dir" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_config_rejects_invalid_log_level(self, aiohttp_client):
        """PUT /api/config with invalid log_level returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/config", json={"log_level": "GARBAGE"})
        assert resp.status == 400
        body = await resp.json()
        assert "log_level" in body["error"].lower() or "invalid" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_config_rejects_invalid_host(self, aiohttp_client):
        """PUT /api/config with host containing special chars returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put("/api/config", json={"host": "bad;host&rm -rf /"})
        assert resp.status == 400
        body = await resp.json()
        assert "host" in body["error"].lower() or "invalid" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_config_rejects_bad_alert_regex(self, aiohttp_client):
        """PUT /api/config with invalid regex in alert_patterns returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)

        resp = await client.put(
            "/api/config",
            json={"alert_patterns": [{"pattern": "[invalid(regex", "severity": "warning"}]},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "regex" in body["error"].lower() or "alert_patterns" in body["error"].lower()


# ── WebSocket Rate Check ──


class TestWebSocketRateCheck:
    def test_ws_rate_check_allows_normal(self):
        """WebSocketHub._ws_rate_check allows normal operations when rate limiter permits."""
        app = _make_test_app()
        hub = app["ws_hub"]

        # Create a mock WebSocket response
        mock_ws = MagicMock(spec=web.WebSocketResponse)
        hub.client_meta[mock_ws] = {"user_id": "test-user", "org_id": "org1"}

        # The app rate limiter is set to always allow in _make_test_app,
        # but _ws_rate_check accesses app["rate_limiter"] via hub.app.
        # Restore real rate limiter for this test.
        real_rl = ashlr_server.RateLimiter()
        app["rate_limiter"] = real_rl

        result = hub._ws_rate_check(mock_ws, "spawn")
        assert result is True

    def test_ws_rate_check_blocks_when_exhausted(self):
        """WebSocketHub._ws_rate_check blocks when rate limit is exhausted."""
        app = _make_test_app()
        hub = app["ws_hub"]

        mock_ws = MagicMock(spec=web.WebSocketResponse)
        hub.client_meta[mock_ws] = {"user_id": "test-user", "org_id": "org1"}

        # Use a real rate limiter and exhaust it
        real_rl = ashlr_server.RateLimiter()
        app["rate_limiter"] = real_rl

        # Exhaust the spawn rate limit (rate=2.0, burst=5.0)
        for _ in range(6):
            hub._ws_rate_check(mock_ws, "spawn")

        # Next check should be blocked
        result = hub._ws_rate_check(mock_ws, "spawn")
        assert result is False

    def test_ws_rate_check_no_rate_limiter_allows(self):
        """WebSocketHub._ws_rate_check allows everything when no rate limiter is set."""
        app = _make_test_app()
        hub = app["ws_hub"]

        # Remove rate limiter from app
        del app["rate_limiter"]

        mock_ws = MagicMock(spec=web.WebSocketResponse)
        hub.client_meta[mock_ws] = {"user_id": "test-user", "org_id": "org1"}

        result = hub._ws_rate_check(mock_ws, "spawn")
        assert result is True
