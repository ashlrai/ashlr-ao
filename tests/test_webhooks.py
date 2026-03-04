"""Tests for webhook endpoints and related features."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

import ashlr_server
from ashlr_ao.server import _validate_webhook_url
from tests.conftest import make_mock_db, make_test_app


class TestWebhookCRUD(AioHTTPTestCase):
    """Tests for webhook CRUD endpoints."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_list_webhooks_empty(self):
        resp = await self.client.get("/api/webhooks")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, [])

    @unittest_run_loop
    async def test_list_webhooks_returns_items(self):
        self.app["db"].get_webhooks = AsyncMock(return_value=[
            {"id": "wh1", "url": "https://example.com/hook", "name": "Test", "events": [], "active": True, "secret": "s3cret"},
        ])
        resp = await self.client.get("/api/webhooks")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data), 1)
        # Secret should be masked
        self.assertEqual(data[0]["secret"], "***")

    @unittest_run_loop
    async def test_create_webhook_valid(self):
        resp = await self.client.post("/api/webhooks", json={
            "url": "https://hooks.example.com/notify",
            "name": "My Webhook",
            "events": ["agent_spawned", "agent_killed"],
        })
        self.assertEqual(resp.status, 201)
        data = await resp.json()
        self.assertIn("id", data)
        self.app["db"].save_webhook.assert_called_once()

    @unittest_run_loop
    async def test_create_webhook_missing_url(self):
        resp = await self.client.post("/api/webhooks", json={"name": "No URL"})
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_create_webhook_invalid_json(self):
        resp = await self.client.post("/api/webhooks", data="not json",
                                       headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_create_webhook_blocked_url_localhost(self):
        resp = await self.client.post("/api/webhooks", json={
            "url": "http://localhost:8080/hook",
        })
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertIn("error", data)

    @unittest_run_loop
    async def test_create_webhook_blocked_url_private_ip(self):
        resp = await self.client.post("/api/webhooks", json={
            "url": "http://192.168.1.100/hook",
        })
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_update_webhook(self):
        self.app["db"].get_webhook = AsyncMock(return_value={
            "id": "wh1", "url": "https://old.example.com/hook", "name": "Old",
            "events": [], "active": True, "secret": "",
        })
        resp = await self.client.put("/api/webhooks/wh1", json={
            "name": "Updated Name",
            "active": False,
        })
        self.assertEqual(resp.status, 200)
        self.app["db"].save_webhook.assert_called_once()

    @unittest_run_loop
    async def test_update_webhook_not_found(self):
        resp = await self.client.put("/api/webhooks/nonexistent", json={"name": "X"})
        self.assertEqual(resp.status, 404)

    @unittest_run_loop
    async def test_delete_webhook_success(self):
        self.app["db"].delete_webhook = AsyncMock(return_value=True)
        resp = await self.client.delete("/api/webhooks/wh1")
        self.assertEqual(resp.status, 200)

    @unittest_run_loop
    async def test_delete_webhook_not_found(self):
        resp = await self.client.delete("/api/webhooks/nonexistent")
        self.assertEqual(resp.status, 404)


class TestWebhookTest(AioHTTPTestCase):
    """Tests for webhook test endpoint."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_test_webhook_not_found(self):
        resp = await self.client.post("/api/webhooks/nonexistent/test")
        self.assertEqual(resp.status, 404)

    @unittest_run_loop
    async def test_test_webhook_success(self):
        self.app["db"].get_webhook = AsyncMock(return_value={
            "id": "wh1", "url": "https://hooks.example.com/test",
            "name": "Test", "events": [], "active": True, "secret": "",
        })
        with patch("aiohttp.ClientSession.post") as mock_post:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_post.return_value = mock_resp
            resp = await self.client.post("/api/webhooks/wh1/test")
            self.assertEqual(resp.status, 200)


class TestWebhookDeliveries(AioHTTPTestCase):
    """Tests for webhook delivery history."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_deliveries_not_found(self):
        resp = await self.client.get("/api/webhooks/nonexistent/deliveries")
        self.assertEqual(resp.status, 404)

    @unittest_run_loop
    async def test_deliveries_empty(self):
        self.app["db"].get_webhook = AsyncMock(return_value={
            "id": "wh1", "url": "https://hooks.example.com/test",
            "name": "Test", "events": [], "active": True, "secret": "",
        })
        resp = await self.client.get("/api/webhooks/wh1/deliveries")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, [])

    @unittest_run_loop
    async def test_deliveries_with_items(self):
        self.app["db"].get_webhook = AsyncMock(return_value={
            "id": "wh1", "url": "https://hooks.example.com/test",
            "name": "Test", "events": [], "active": True, "secret": "",
        })
        self.app["db"].get_webhook_deliveries = AsyncMock(return_value=[
            {"id": 1, "webhook_id": "wh1", "event_type": "agent_spawned",
             "payload_json": "{}", "status": "delivered", "attempts": 1},
        ])
        resp = await self.client.get("/api/webhooks/wh1/deliveries")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data), 1)


class TestValidateWebhookUrl(AioHTTPTestCase):
    """Tests for SSRF protection in webhook URL validation."""

    async def get_application(self):
        return make_test_app()

    def test_valid_https_url(self):
        result = _validate_webhook_url("https://example.com/webhook")
        self.assertIsNone(result)

    def test_valid_http_url(self):
        result = _validate_webhook_url("http://example.com/webhook")
        self.assertIsNone(result)

    def test_blocks_localhost(self):
        result = _validate_webhook_url("http://localhost/hook")
        self.assertIsNotNone(result)

    def test_blocks_127_0_0_1(self):
        result = _validate_webhook_url("http://127.0.0.1/hook")
        self.assertIsNotNone(result)

    def test_blocks_private_10_x(self):
        result = _validate_webhook_url("http://10.0.0.1/hook")
        self.assertIsNotNone(result)

    def test_blocks_private_172_16(self):
        result = _validate_webhook_url("http://172.16.0.1/hook")
        self.assertIsNotNone(result)

    def test_blocks_ipv6_loopback(self):
        result = _validate_webhook_url("http://[::1]/hook")
        self.assertIsNotNone(result)

    def test_rejects_ftp_scheme(self):
        result = _validate_webhook_url("ftp://example.com/hook")
        self.assertIsNotNone(result)

    def test_rejects_no_hostname(self):
        result = _validate_webhook_url("https:///hook")
        self.assertIsNotNone(result)


class TestEventExport(AioHTTPTestCase):
    """Tests for event audit log export."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_export_json_default(self):
        self.app["db"].get_events = AsyncMock(return_value=[
            {"id": 1, "event": "agent_spawned", "message": "Agent x spawned",
             "agent_id": "a1", "agent_name": "x", "created_at": "2026-01-01T00:00:00"},
        ])
        resp = await self.client.get("/api/events/export")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data), 1)

    @unittest_run_loop
    async def test_export_json_explicit(self):
        self.app["db"].get_events = AsyncMock(return_value=[])
        resp = await self.client.get("/api/events/export?format=json")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, [])

    @unittest_run_loop
    async def test_export_csv(self):
        self.app["db"].get_events = AsyncMock(return_value=[
            {"id": 1, "event_type": "agent_spawned", "message": "Agent x spawned",
             "agent_id": "a1", "agent_name": "x", "created_at": "2026-01-01T00:00:00"},
        ])
        resp = await self.client.get("/api/events/export?format=csv")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("event_type", text)  # CSV header
        self.assertIn("agent_spawned", text)

    @unittest_run_loop
    async def test_export_with_limit(self):
        self.app["db"].get_events = AsyncMock(return_value=[])
        resp = await self.client.get("/api/events/export?limit=10")
        self.assertEqual(resp.status, 200)

    @unittest_run_loop
    async def test_export_with_event_type_filter(self):
        self.app["db"].get_events = AsyncMock(return_value=[])
        resp = await self.client.get("/api/events/export?event_type=agent_spawned")
        self.assertEqual(resp.status, 200)


class TestAgentOutputHistory(AioHTTPTestCase):
    """Tests for terminal output history endpoint."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_output_history_no_agent_returns_empty(self):
        resp = await self.client.get("/api/agents/nonexistent/output-history")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["lines"], [])

    @unittest_run_loop
    async def test_output_history_with_agent(self):
        mgr = self.app["agent_manager"]
        agent = ashlr_server.Agent(id="a1", name="test", role="general", working_dir="/tmp", backend="claude-code", status="working", task="test task")
        agent.output_lines = ["line 1", "line 2", "line 3"]
        mgr.agents["a1"] = agent
        resp = await self.client.get("/api/agents/a1/output-history")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("lines", data)
        self.assertEqual(len(data["lines"]), 3)
        self.assertEqual(data["total_memory"], 3)

    @unittest_run_loop
    async def test_output_history_pagination(self):
        mgr = self.app["agent_manager"]
        agent = ashlr_server.Agent(id="a1", name="test", role="general", working_dir="/tmp", backend="claude-code", status="working", task="test task")
        agent.output_lines = [f"line {i}" for i in range(100)]
        mgr.agents["a1"] = agent
        resp = await self.client.get("/api/agents/a1/output-history?offset=0&limit=5")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("lines", data)
        self.assertEqual(data["limit"], 5)


class TestAgentOutputSearch(AioHTTPTestCase):
    """Tests for terminal output search endpoint."""

    async def get_application(self):
        return make_test_app()

    def _add_agent(self, lines=None):
        mgr = self.app["agent_manager"]
        agent = ashlr_server.Agent(id="a1", name="test", role="general", working_dir="/tmp", backend="claude-code", status="working", task="test task")
        if lines:
            agent.output_lines = lines
        mgr.agents["a1"] = agent
        return agent

    @unittest_run_loop
    async def test_output_search_agent_not_found(self):
        resp = await self.client.get("/api/agents/nonexistent/output-search?q=test")
        self.assertEqual(resp.status, 404)

    @unittest_run_loop
    async def test_output_search_invalid_regex(self):
        self._add_agent(["test line"])
        resp = await self.client.get("/api/agents/a1/output-search?q=[invalid&regex=true")
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertIn("error", data)

    @unittest_run_loop
    async def test_output_search_no_query(self):
        self._add_agent(["test line"])
        resp = await self.client.get("/api/agents/a1/output-search")
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_output_search_with_results(self):
        self._add_agent(["error: something failed", "success", "error: another failure"])
        resp = await self.client.get("/api/agents/a1/output-search?q=error")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("matches", data)
        self.assertGreaterEqual(len(data["matches"]), 2)


class TestModelPricing(AioHTTPTestCase):
    """Tests for per-model cost pricing."""

    async def get_application(self):
        return make_test_app()

    def test_get_model_pricing_exact_match(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("claude-opus-4-6")
        self.assertEqual(rate, (0.015, 0.075))

    def test_get_model_pricing_short_alias(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("sonnet")
        self.assertEqual(rate, (0.003, 0.015))

    def test_get_model_pricing_fuzzy_match(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("claude-sonnet-4-6-latest")
        self.assertEqual(rate, (0.003, 0.015))

    def test_get_model_pricing_unknown_model(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("unknown-model-xyz")
        # Should fall back to backend default
        self.assertIsNotNone(rate)

    def test_get_model_pricing_openai(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("gpt-4o")
        self.assertEqual(rate, (0.0025, 0.01))

    def test_get_model_pricing_gpt4o_mini(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("gpt-4o-mini")
        self.assertEqual(rate, (0.00015, 0.0006))

    def test_get_model_pricing_haiku(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("haiku")
        self.assertEqual(rate, (0.0008, 0.004))

    def test_get_model_pricing_default_fallback(self):
        from ashlr_ao.backends import get_model_pricing
        rate = get_model_pricing("", "nonexistent-backend")
        self.assertEqual(rate, (0.003, 0.015))  # default

    @unittest_run_loop
    async def test_costs_endpoint_enhanced(self):
        """Test that /api/costs returns per-project breakdown and budget info."""
        mgr = self.app["agent_manager"]
        agent = ashlr_server.Agent(id="a1", name="test", role="general", working_dir="/tmp", backend="claude-code", status="working", task="test task")
        agent.estimated_cost_usd = 0.05
        agent.tokens_input = 1000
        agent.tokens_output = 500
        agent.project_id = "p1"
        mgr.agents["a1"] = agent
        self.app["config"].cost_budget_usd = 1.0
        resp = await self.client.get("/api/costs")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("total_cost_usd", data)
        self.assertIn("by_project", data)
        self.assertIn("budget", data)
        self.assertEqual(data["budget"]["budget_usd"], 1.0)
        self.assertAlmostEqual(data["by_project"]["p1"], 0.05)


class TestWebhookBroadcast(AioHTTPTestCase):
    """Tests for webhook event broadcasting integration."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_broadcast_event_queues_webhooks(self):
        """Verify broadcast_event queues for webhook delivery."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[
            {"id": "wh1", "url": "https://example.com/hook", "events": [], "active": True},
        ])
        await hub.broadcast_event("agent_spawned", "Agent x spawned", "a1", "x")
        db.queue_webhook_delivery.assert_called_once()

    @unittest_run_loop
    async def test_broadcast_event_filters_by_event_type(self):
        """Verify webhook event filtering works."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[
            {"id": "wh1", "url": "https://example.com/hook", "events": ["agent_killed"], "active": True},
        ])
        await hub.broadcast_event("agent_spawned", "Agent x spawned", "a1", "x")
        db.queue_webhook_delivery.assert_not_called()

    @unittest_run_loop
    async def test_broadcast_event_redacts_secrets(self):
        """Verify secrets are redacted from event messages."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[])
        # Message containing what looks like an API key
        msg = "Using key sk-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        await hub.broadcast_event("test", msg, "a1", "test")
        call_args = db.log_event.call_args
        logged_msg = call_args[0][1]  # second positional arg is message
        self.assertNotIn("sk-abc123", logged_msg)

    @unittest_run_loop
    async def test_broadcast_event_uses_cached_webhooks(self):
        """Verify webhook cache avoids repeated DB queries."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[
            {"id": "wh1", "url": "https://example.com/hook", "events": [], "active": True},
        ])
        # First call should query DB
        await hub.broadcast_event("test1", "msg1", "a1", "x")
        self.assertEqual(db.get_webhooks.call_count, 1)
        # Second call within TTL should use cache
        await hub.broadcast_event("test2", "msg2", "a1", "x")
        self.assertEqual(db.get_webhooks.call_count, 1)  # still 1, cached

    @unittest_run_loop
    async def test_webhook_cache_invalidated_on_create(self):
        """Verify cache is invalidated when a webhook is created."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[])
        await hub.broadcast_event("test", "msg", "a1", "x")
        self.assertEqual(db.get_webhooks.call_count, 1)
        # Create webhook invalidates cache
        resp = await self.client.post("/api/webhooks", json={
            "url": "https://example.com/hook", "name": "test"
        })
        self.assertEqual(resp.status, 201)
        # Next broadcast should re-query DB
        await hub.broadcast_event("test2", "msg2", "a1", "x")
        self.assertEqual(db.get_webhooks.call_count, 2)

    @unittest_run_loop
    async def test_webhook_cache_invalidated_on_delete(self):
        """Verify cache is invalidated when a webhook is deleted."""
        hub = self.app["ws_hub"]
        db = self.app["db"]
        db.get_webhooks = AsyncMock(return_value=[])
        db.delete_webhook = AsyncMock(return_value=True)
        await hub.broadcast_event("test", "msg", "a1", "x")
        resp = await self.client.delete("/api/webhooks/wh1")
        self.assertEqual(resp.status, 200)
        await hub.broadcast_event("test2", "msg2", "a1", "x")
        self.assertEqual(db.get_webhooks.call_count, 2)


class TestWebhookDeliveryHistory(AioHTTPTestCase):
    """Tests for webhook delivery history endpoint."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_deliveries_not_found(self):
        resp = await self.client.get("/api/webhooks/nonexistent/deliveries")
        self.assertEqual(resp.status, 404)

    @unittest_run_loop
    async def test_deliveries_empty(self):
        self.app["db"].get_webhook = AsyncMock(return_value={"id": "wh1"})
        self.app["db"].get_webhook_deliveries = AsyncMock(return_value=[])
        resp = await self.client.get("/api/webhooks/wh1/deliveries")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, [])

    @unittest_run_loop
    async def test_deliveries_returns_items(self):
        self.app["db"].get_webhook = AsyncMock(return_value={"id": "wh1"})
        deliveries = [
            {"id": "d1", "event": "agent_spawned", "status": "delivered", "http_status": 200},
            {"id": "d2", "event": "agent_killed", "status": "failed", "http_status": 500},
        ]
        self.app["db"].get_webhook_deliveries = AsyncMock(return_value=deliveries)
        resp = await self.client.get("/api/webhooks/wh1/deliveries")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(len(data), 2)


class TestRateLimitsExpensiveEndpoints(AioHTTPTestCase):
    """Tests that expensive endpoints have rate limiting."""

    async def get_application(self):
        app = make_test_app()
        # Enable rate limiter for these tests
        from ashlr_ao.middleware import RateLimiter
        app["rate_limiter"] = RateLimiter()
        return app

    @unittest_run_loop
    async def test_export_events_rate_limited(self):
        """Export endpoint should reject after burst is exhausted."""
        # burst=3, rate=0.2/sec, cost=3 per call → only 1 call before exhaustion
        resp = await self.client.get("/api/events/export")
        self.assertEqual(resp.status, 200)
        # Hit rate limit
        for _ in range(3):
            resp = await self.client.get("/api/events/export")
        self.assertEqual(resp.status, 429)

    @unittest_run_loop
    async def test_summarize_rate_limited(self):
        """Summarize endpoint should have rate limiting."""
        import time
        manager = self.app["agent_manager"]
        agent = ashlr_server.Agent(
            id="a1b2", name="test", role="general",
            working_dir="/tmp/test", backend="claude-code",
            status="working", task="test",
            tmux_session="ashlr-a1b2",
        )
        agent._spawn_time = time.monotonic() - 60
        manager.agents["a1b2"] = agent
        # First call — agent exists but no LLM client, so may return 503/400
        resp = await self.client.post("/api/agents/a1b2/summarize")
        # Should NOT be 429 — it got past the rate limiter
        self.assertNotEqual(resp.status, 429)
        # Exhaust burst
        for _ in range(5):
            resp = await self.client.post("/api/agents/a1b2/summarize")
        self.assertEqual(resp.status, 429)


class TestSystemDiagnostics(AioHTTPTestCase):
    """Tests for system diagnostics enhancements."""

    async def get_application(self):
        return make_test_app()

    @unittest_run_loop
    async def test_system_metrics_includes_uptime(self):
        """System endpoint should include uptime_sec."""
        resp = await self.client.get("/api/system")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("uptime_sec", data)
        self.assertIsInstance(data["uptime_sec"], (int, float))
        self.assertGreaterEqual(data["uptime_sec"], 0)
