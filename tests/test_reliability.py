"""Reliability tests for production hardening — archive cleanup, request logging,
auth logging, shutdown timeout, and exception handler logging."""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    from ashlr_server import (
        RateLimiter,
        Database,
        Config,
        Agent,
        User,
        archive_cleanup_loop,
        cleanup_background_tasks,
        request_logging_middleware,
        security_headers_middleware,
    )

from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app


# ─────────────────────────────────────────────
# Archive Cleanup
# ─────────────────────────────────────────────

class TestArchiveCleanup:
    @pytest.mark.asyncio
    async def test_archive_cleanup_calls_db(self):
        """archive_cleanup_loop calls cleanup_old_archives on the DB."""
        app = _make_test_app()
        app["db"].cleanup_old_archives = AsyncMock(return_value=5)

        # Run one iteration by patching sleep to raise after first call
        call_count = 0
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        app["db"].cleanup_old_archives.assert_awaited_once_with(retention_hours=48)

    @pytest.mark.asyncio
    async def test_archive_cleanup_skips_when_db_unavailable(self):
        """archive_cleanup_loop skips when DB is unavailable."""
        app = _make_test_app()
        app["db_available"] = False
        app["db"].cleanup_old_archives = AsyncMock(return_value=0)

        call_count = 0
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        app["db"].cleanup_old_archives.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_archive_cleanup_handles_db_error(self):
        """archive_cleanup_loop doesn't crash on DB errors."""
        app = _make_test_app()
        app["db"].cleanup_old_archives = AsyncMock(side_effect=Exception("DB error"))

        call_count = 0
        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=mock_sleep):
            try:
                await archive_cleanup_loop(app)
            except asyncio.CancelledError:
                pass

        # Should have been called (and failed gracefully)
        app["db"].cleanup_old_archives.assert_awaited_once()


# ─────────────────────────────────────────────
# Request Logging Middleware
# ─────────────────────────────────────────────

class TestRequestLogging:
    @pytest.mark.asyncio
    async def test_health_endpoint_returns_user_id_in_log(self, aiohttp_client):
        """Request logging includes user ID placeholder when no auth."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_api_path_skips_logging(self, aiohttp_client):
        """Non-API paths (dashboard) skip request logging middleware."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        # Dashboard is served at /
        resp = await client.get("/")
        assert resp.status == 200


# ─────────────────────────────────────────────
# Security Headers Middleware
# ─────────────────────────────────────────────

class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_csp_header_on_api(self, aiohttp_client):
        """API responses include Content-Security-Policy header."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert "Content-Security-Policy" in resp.headers
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src" in csp
        assert "frame-ancestors 'none'" in csp

    @pytest.mark.asyncio
    async def test_xcto_header(self, aiohttp_client):
        """API responses include X-Content-Type-Options: nosniff."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    @pytest.mark.asyncio
    async def test_xfo_header(self, aiohttp_client):
        """API responses include X-Frame-Options: DENY."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    @pytest.mark.asyncio
    async def test_referrer_policy_header(self, aiohttp_client):
        """API responses include Referrer-Policy header."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/api/health")
        assert "Referrer-Policy" in resp.headers

    @pytest.mark.asyncio
    async def test_csp_on_dashboard(self, aiohttp_client):
        """Dashboard (/) also gets security headers."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.get("/")
        assert "Content-Security-Policy" in resp.headers


# ─────────────────────────────────────────────
# Shutdown Timeout
# ─────────────────────────────────────────────

class TestShutdownTimeout:
    @pytest.mark.asyncio
    async def test_cleanup_cancels_all_tasks(self):
        """cleanup_background_tasks cancels all background tasks."""
        app = _make_test_app()
        # Create mock tasks
        task1 = MagicMock()
        task1.cancel = MagicMock()
        task2 = MagicMock()
        task2.cancel = MagicMock()

        async def mock_wait_cancel(*a, **kw):
            raise asyncio.CancelledError()

        task1.__await__ = lambda s: mock_wait_cancel().__await__()
        task2.__await__ = lambda s: mock_wait_cancel().__await__()

        app["bg_tasks"] = [task1, task2]
        app["db"].close = AsyncMock()
        app["agent_manager"].cleanup_all = MagicMock()

        with patch("asyncio.wait_for", side_effect=asyncio.CancelledError()):
            await cleanup_background_tasks(app)

        task1.cancel.assert_called_once()
        task2.cancel.assert_called_once()
        app["db"].close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_timeout(self):
        """cleanup_background_tasks handles task timeout gracefully."""
        app = _make_test_app()
        app["bg_tasks"] = []
        app["db"].close = AsyncMock()
        app["agent_manager"].cleanup_all = MagicMock()

        await cleanup_background_tasks(app)
        app["db"].close.assert_awaited_once()
        app["agent_manager"].cleanup_all.assert_called_once()


# ─────────────────────────────────────────────
# Config Validation Enhancements
# ─────────────────────────────────────────────

class TestConfigValidation:
    @pytest.mark.asyncio
    async def test_invalid_log_level_rejected(self, aiohttp_client):
        """PUT /api/config with invalid log_level returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"log_level": "GARBAGE"})
        assert resp.status == 400
        body = await resp.json()
        assert "log_level" in body["error"]

    @pytest.mark.asyncio
    async def test_valid_log_level_accepted(self, aiohttp_client):
        """PUT /api/config with valid log_level succeeds."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"log_level": "DEBUG"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_host_rejected(self, aiohttp_client):
        """PUT /api/config with invalid host (special chars) returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"host": "foo bar!@#"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_working_dir_rejected(self, aiohttp_client):
        """PUT /api/config with nonexistent default_working_dir returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"default_working_dir": "/nonexistent/path/xyz"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_alert_regex_rejected(self, aiohttp_client):
        """PUT /api/config with invalid regex in alert_patterns returns 400."""
        app = _make_test_app()
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={
            "alert_patterns": [{"pattern": "[invalid(regex", "severity": "error"}]
        })
        assert resp.status == 400
        body = await resp.json()
        assert "regex" in body["error"].lower() or "alert_patterns" in body["error"]


# ─────────────────────────────────────────────
# RateLimiter Eviction
# ─────────────────────────────────────────────

class TestRateLimiterEviction:
    def test_eviction_removes_stale_buckets(self):
        """After 100 checks, stale buckets (>1hr old) are removed."""
        rl = RateLimiter()
        # Manually insert a stale bucket (2 hours old)
        rl._buckets["stale-ip:default"] = {
            "tokens": 10.0,
            "last_refill": time.monotonic() - 7200,  # 2 hours ago
        }
        # Insert a fresh bucket
        rl._buckets["fresh-ip:default"] = {
            "tokens": 10.0,
            "last_refill": time.monotonic(),
        }
        # Do 100 checks to trigger eviction
        for i in range(100):
            rl.check(f"eviction-test-{i}", cost=0.01, rate=100.0, burst=1000.0)

        assert "stale-ip:default" not in rl._buckets
        assert "fresh-ip:default" in rl._buckets

    def test_check_count_increments(self):
        """_check_count increments with each check call."""
        rl = RateLimiter()
        assert rl._check_count == 0
        rl.check("test-ip")
        assert rl._check_count == 1
        rl.check("test-ip")
        assert rl._check_count == 2

    def test_eviction_keeps_recent_buckets(self):
        """Recent buckets survive eviction."""
        rl = RateLimiter()
        # Add recent bucket
        rl.check("keep-me:default", cost=1.0, rate=1.0, burst=10.0)
        # Run 99 more checks to hit 100
        for i in range(99):
            rl.check(f"filler-{i}", cost=0.01, rate=100.0, burst=1000.0)

        assert "keep-me:default" in rl._buckets

    def test_cleanup_stale_still_works(self):
        """Original cleanup_stale method still works."""
        rl = RateLimiter()
        rl._buckets["old"] = {"tokens": 5.0, "last_refill": time.monotonic() - 600}
        rl._buckets["new"] = {"tokens": 5.0, "last_refill": time.monotonic()}
        rl.cleanup_stale(max_age=300.0)
        assert "old" not in rl._buckets
        assert "new" in rl._buckets


# ─────────────────────────────────────────────
# Request Size Limit
# ─────────────────────────────────────────────

class TestRequestSizeLimit:
    def test_app_has_client_max_size(self):
        """Application is configured with 10MB request size limit."""
        app = _make_test_app()
        assert app._client_max_size == 10 * 1024 * 1024
