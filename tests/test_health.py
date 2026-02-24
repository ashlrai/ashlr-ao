"""Tests for health scoring and rate limiting."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
with patch("psutil.cpu_percent", return_value=0.0):
    from ashlar_server import calculate_health_score, RateLimiter


# ─────────────────────────────────────────────
# calculate_health_score
# ─────────────────────────────────────────────

class TestCalculateHealthScore:
    def test_returns_float_between_0_and_1(self, make_agent):
        agent = make_agent()
        score = calculate_health_score(agent)
        assert 0.0 <= score <= 1.0

    def test_healthy_agent_high_score(self, make_agent):
        """Agent with no errors, recent output, low memory should score high."""
        agent = make_agent(error_count=0, memory_mb=100)
        score = calculate_health_score(agent, memory_limit_mb=2048)
        assert score > 0.7

    def test_many_errors_lower_score(self, make_agent):
        agent_good = make_agent(error_count=0)
        agent_bad = make_agent(error_count=20)
        score_good = calculate_health_score(agent_good)
        score_bad = calculate_health_score(agent_bad)
        assert score_good > score_bad

    def test_error_status_caps_score(self, make_agent):
        """Agent in error status should have very low score."""
        agent = make_agent(status="error", error_count=5)
        score = calculate_health_score(agent)
        assert score < 0.2

    def test_paused_returns_half(self, make_agent):
        agent = make_agent(status="paused")
        score = calculate_health_score(agent)
        assert score == 0.5

    def test_high_memory_penalized(self, make_agent):
        agent_low = make_agent(memory_mb=500)
        agent_high = make_agent(memory_mb=1900)
        score_low = calculate_health_score(agent_low, memory_limit_mb=2048)
        score_high = calculate_health_score(agent_high, memory_limit_mb=2048)
        assert score_low > score_high

    def test_stale_output_penalized(self, make_agent):
        """Agent with no recent output should score lower."""
        agent_fresh = make_agent()
        agent_fresh.last_output_time = time.monotonic() - 5  # 5s ago

        agent_stale = make_agent()
        agent_stale.last_output_time = time.monotonic() - 200  # 200s ago

        score_fresh = calculate_health_score(agent_fresh)
        score_stale = calculate_health_score(agent_stale)
        assert score_fresh > score_stale

    def test_new_agent_reasonable_score(self, make_agent):
        """Just-spawned agent with no output yet should get reasonable score."""
        agent = make_agent()
        agent._spawn_time = time.monotonic() - 2  # spawned 2s ago
        agent.last_output_time = 0  # no output yet
        score = calculate_health_score(agent)
        assert 0.3 < score < 0.9

    def test_zero_memory_limit(self, make_agent):
        """Edge case: zero memory limit shouldn't crash."""
        agent = make_agent(memory_mb=100)
        score = calculate_health_score(agent, memory_limit_mb=0)
        assert 0.0 <= score <= 1.0

    def test_no_spawn_time(self, make_agent):
        agent = make_agent()
        agent._spawn_time = 0
        score = calculate_health_score(agent)
        assert 0.0 <= score <= 1.0


# ─────────────────────────────────────────────
# RateLimiter
# ─────────────────────────────────────────────

class TestRateLimiter:
    def test_first_request_allowed(self):
        rl = RateLimiter()
        allowed, retry = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        assert allowed is True
        assert retry == 0.0

    def test_burst_limit(self):
        rl = RateLimiter()
        # Drain the burst bucket
        for _ in range(10):
            allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
            assert allowed is True

        # 11th request should be denied
        allowed, retry = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        assert allowed is False
        assert retry > 0.0

    def test_different_ips_independent(self):
        rl = RateLimiter()
        # Drain IP1
        for _ in range(10):
            rl.check("10.0.0.1", cost=1.0, rate=1.0, burst=10.0)

        # IP2 should still work
        allowed, _ = rl.check("10.0.0.2", cost=1.0, rate=1.0, burst=10.0)
        assert allowed is True

    def test_tokens_refill_over_time(self):
        rl = RateLimiter()
        # Drain bucket
        for _ in range(5):
            rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=5.0)

        # Should be denied
        allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=5.0)
        assert allowed is False

        # Simulate time passing by manipulating the bucket
        rl._buckets["127.0.0.1"]["last_refill"] = time.monotonic() - 5.0

        # Now should have refilled tokens
        allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=5.0)
        assert allowed is True

    def test_high_cost_request(self):
        rl = RateLimiter()
        # High cost should drain more tokens
        allowed, _ = rl.check("127.0.0.1", cost=5.0, rate=1.0, burst=10.0)
        assert allowed is True

        # After 5-cost request, only 5 tokens remain
        for _ in range(5):
            rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)

        allowed, _ = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=10.0)
        assert allowed is False

    def test_cleanup_stale(self):
        rl = RateLimiter()
        rl.check("stale-ip", cost=1.0)
        # Make it stale
        rl._buckets["stale-ip"]["last_refill"] = time.monotonic() - 600

        rl.check("fresh-ip", cost=1.0)

        rl.cleanup_stale(max_age=300.0)

        assert "stale-ip" not in rl._buckets
        assert "fresh-ip" in rl._buckets

    def test_retry_after_calculation(self):
        rl = RateLimiter()
        # Drain completely
        for _ in range(3):
            rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=3.0)

        allowed, retry = rl.check("127.0.0.1", cost=1.0, rate=1.0, burst=3.0)
        assert allowed is False
        assert retry > 0.0
        assert retry <= 1.5  # Should be roughly cost/rate = 1.0, with some tolerance
