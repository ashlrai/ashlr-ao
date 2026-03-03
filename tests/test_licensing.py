"""Tests for open-core licensing system: License dataclass, validate_license(),
_effective_max_agents, _check_feature, DB methods, and HTTP endpoints."""

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

with patch("psutil.cpu_percent", return_value=0.0):
    import ashlr_server
    import ashlr_ao.server as _server_mod  # For patching module-level constants
    import ashlr_ao.licensing as _licensing_mod  # Canonical location of LICENSE_PUBLIC_KEY_PEM
    from ashlr_server import (
        License, COMMUNITY_LICENSE, PRO_FEATURES, validate_license,
        _effective_max_agents, _check_feature, Organization, Database,
        Config, RateLimiter,
    )

TEST_WORKING_DIR = str(Path.home())


# ── Test keypair fixture ──

def _generate_test_keypair():
    """Generate a fresh Ed25519 keypair for test JWT signing."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem, private_key


TEST_PRIV_PEM, TEST_PUB_PEM, TEST_PRIVATE_KEY = _generate_test_keypair()


def _sign_license(payload: dict) -> str:
    """Sign a JWT license with the test private key."""
    return jwt.encode(payload, TEST_PRIVATE_KEY, algorithm="EdDSA")


def _make_pro_payload(org_id="test-org", days=365, max_agents=100, max_seats=50):
    """Create a valid Pro license JWT payload."""
    now = datetime.now(timezone.utc)
    return {
        "sub": org_id,
        "iss": "ashlr-licensing",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=days)).timestamp()),
        "tier": "pro",
        "max_agents": max_agents,
        "max_seats": max_seats,
        "features": list(PRO_FEATURES),
    }


def _make_expired_payload(org_id="test-org"):
    """Create an expired Pro license JWT payload."""
    past = datetime.now(timezone.utc) - timedelta(days=30)
    return {
        "sub": org_id,
        "iss": "ashlr-licensing",
        "iat": int((past - timedelta(days=365)).timestamp()),
        "exp": int(past.timestamp()),
        "tier": "pro",
        "max_agents": 100,
        "max_seats": 50,
        "features": list(PRO_FEATURES),
    }




# ─────────────────────────────────────────────
# License Dataclass Tests
# ─────────────────────────────────────────────

class TestLicenseDataclass:
    def test_community_defaults(self):
        lic = License()
        assert lic.tier == "community"
        assert lic.max_agents == 5
        assert lic.max_seats == 1
        assert lic.features == frozenset()
        assert lic.is_pro is False
        assert lic.is_expired is False  # Community never expires

    def test_community_license_singleton(self):
        assert COMMUNITY_LICENSE.tier == "community"
        assert COMMUNITY_LICENSE.max_agents == 5
        assert COMMUNITY_LICENSE.is_pro is False

    def test_pro_license_not_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        lic = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        assert lic.is_pro is True
        assert lic.is_expired is False

    def test_pro_license_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        lic = License(tier="pro", max_agents=100, expires_at=past, features=PRO_FEATURES)
        assert lic.is_pro is False
        assert lic.is_expired is True

    def test_to_dict_fields(self):
        lic = License(tier="pro", max_agents=50, max_seats=10, org_id="o1",
                      features=frozenset({"intelligence", "workflows"}),
                      issued_at="2026-01-01T00:00:00+00:00",
                      expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
        d = lic.to_dict()
        assert d["tier"] == "pro"
        assert d["max_agents"] == 50
        assert d["max_seats"] == 10
        assert "raw_key" not in d  # raw_key should not be in to_dict
        assert isinstance(d["features"], list)
        assert d["is_pro"] is True
        assert d["is_expired"] is False

    def test_to_dict_excludes_raw_key(self):
        lic = License(raw_key="secret-jwt-token")
        d = lic.to_dict()
        assert "raw_key" not in d

    def test_is_expired_bad_date(self):
        lic = License(tier="pro", expires_at="not-a-date")
        assert lic.is_expired is True
        assert lic.is_pro is False

    def test_is_expired_no_tz(self):
        """Naive datetime without timezone is treated as UTC."""
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        lic = License(tier="pro", expires_at=future)
        assert lic.is_expired is False


class TestProFeatures:
    def test_pro_features_contains_expected(self):
        assert "multi_user" in PRO_FEATURES
        assert "intelligence" in PRO_FEATURES
        assert "workflows" in PRO_FEATURES
        assert "fleet_presets" in PRO_FEATURES
        assert "unlimited_agents" in PRO_FEATURES

    def test_pro_features_is_frozenset(self):
        assert isinstance(PRO_FEATURES, frozenset)


# ─────────────────────────────────────────────
# validate_license() Tests
# ─────────────────────────────────────────────

class TestValidateLicense:
    def test_empty_key_returns_community(self):
        assert validate_license("").tier == "community"
        assert validate_license(None).tier == "community"
        assert validate_license("   ").tier == "community"

    def test_invalid_jwt_returns_community(self):
        assert validate_license("not.a.jwt").tier == "community"

    def test_tampered_jwt_returns_community(self):
        """A JWT signed with a different key should fail verification."""
        other_key = Ed25519PrivateKey.generate()
        payload = _make_pro_payload()
        token = jwt.encode(payload, other_key, algorithm="EdDSA")
        with patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM):
            result = validate_license(token)
        assert result.tier == "community"

    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    def test_expired_jwt_returns_community(self):
        payload = _make_expired_payload()
        token = _sign_license(payload)
        result = validate_license(token)
        assert result.tier == "community"

    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    def test_valid_pro_jwt(self):
        payload = _make_pro_payload(org_id="acme", max_agents=50, max_seats=25)
        token = _sign_license(payload)
        result = validate_license(token)
        assert result.tier == "pro"
        assert result.is_pro is True
        assert result.max_agents == 50
        assert result.max_seats == 25
        assert result.org_id == "acme"
        assert result.raw_key == token
        assert result.features == PRO_FEATURES

    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    def test_valid_jwt_with_custom_features(self):
        payload = _make_pro_payload()
        payload["features"] = ["intelligence", "workflows"]
        token = _sign_license(payload)
        result = validate_license(token)
        assert result.features == frozenset({"intelligence", "workflows"})

    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    def test_missing_required_claims_returns_community(self):
        """JWT without required claims (exp, iss, sub) should fail."""
        payload = {"tier": "pro", "max_agents": 100}
        token = _sign_license(payload)
        result = validate_license(token)
        assert result.tier == "community"


# ─────────────────────────────────────────────
# _effective_max_agents() Tests
# ─────────────────────────────────────────────

class TestEffectiveMaxAgents:
    def _make_app(self, config_max=16, license=None):
        app = {}
        app["config"] = Config(max_agents=config_max)
        app["license"] = license or COMMUNITY_LICENSE
        return app

    def test_community_capped_at_5(self):
        app = self._make_app(config_max=16)
        assert _effective_max_agents(app) == 5

    def test_community_config_lower_than_5(self):
        app = self._make_app(config_max=3)
        assert _effective_max_agents(app) == 3

    def test_pro_uses_config(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        app = self._make_app(config_max=16, license=pro)
        assert _effective_max_agents(app) == 16

    def test_pro_capped_by_license(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=50, expires_at=future, features=PRO_FEATURES)
        app = self._make_app(config_max=80, license=pro)
        assert _effective_max_agents(app) == 50

    def test_expired_pro_reverts_to_community(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        expired = License(tier="pro", max_agents=100, expires_at=past, features=PRO_FEATURES)
        app = self._make_app(config_max=16, license=expired)
        assert _effective_max_agents(app) == 5


# ─────────────────────────────────────────────
# _check_feature() Tests
# ─────────────────────────────────────────────

class TestCheckFeature:
    def _make_request(self, license=None):
        req = MagicMock()
        req.app = {"license": license or COMMUNITY_LICENSE}
        return req

    def test_community_blocks_pro_features(self):
        req = self._make_request()
        for feat in PRO_FEATURES:
            result = _check_feature(req, feat)
            assert result is not None
            assert result.status == 403

    def test_pro_allows_all_features(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        req = self._make_request(license=pro)
        for feat in PRO_FEATURES:
            assert _check_feature(req, feat) is None

    def test_non_gated_feature_always_allowed(self):
        req = self._make_request()
        assert _check_feature(req, "not_a_real_feature") is None

    def test_community_403_response_body(self):
        req = self._make_request()
        result = _check_feature(req, "intelligence")
        assert result.status == 403
        body = json.loads(result.body)
        assert body["feature"] == "intelligence"
        assert body["current_plan"] == "community"


# ─────────────────────────────────────────────
# Organization license fields
# ─────────────────────────────────────────────

class TestOrganizationLicenseFields:
    def test_org_defaults(self):
        org = Organization(id="o1", name="Acme", slug="acme")
        assert org.license_key == ""
        assert org.plan == "community"

    def test_org_to_dict_includes_plan_excludes_key(self):
        org = Organization(id="o1", name="Acme", slug="acme", license_key="secret", plan="pro")
        d = org.to_dict()
        assert d["plan"] == "pro"
        assert "license_key" not in d


# ─────────────────────────────────────────────
# Database license methods
# ─────────────────────────────────────────────

class TestDatabaseLicenseMethods:
    async def test_update_and_get_org_license(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = Path(f.name)
        f.close()
        db = Database(db_path)
        await db.init()

        org = await db.create_org("Acme", "acme")
        assert org.license_key == ""
        assert org.plan == "community"

        await db.update_org_license(org.id, "test-key-123", "pro")

        key = await db.get_org_license_key(org.id)
        assert key == "test-key-123"

        updated_org = await db.get_org(org.id)
        assert updated_org.license_key == "test-key-123"
        assert updated_org.plan == "pro"

        # Clear license
        await db.update_org_license(org.id, "", "community")
        key2 = await db.get_org_license_key(org.id)
        assert key2 == ""

        await db.close()
        db_path.unlink(missing_ok=True)



# ─────────────────────────────────────────────
# HTTP Endpoint Tests
# ─────────────────────────────────────────────

from conftest import make_mock_db as _make_mock_db, make_test_app as _make_test_app


class TestLicenseStatusEndpoint:
    @pytest.mark.asyncio
    async def test_license_status_community(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.get("/api/license/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["license"]["tier"] == "community"
        assert body["license"]["is_pro"] is False
        assert body["effective_max_agents"] == 5
        assert "gated_features" in body

    @pytest.mark.asyncio
    async def test_license_status_pro(self, aiohttp_client):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        app = _make_test_app(license=pro)
        client = await aiohttp_client(app)
        resp = await client.get("/api/license/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["license"]["tier"] == "pro"
        assert body["license"]["is_pro"] is True
        assert body["effective_max_agents"] == 16  # min(config=16, license=100)


class TestActivateLicenseEndpoint:
    @pytest.mark.asyncio
    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    async def test_activate_valid_key(self, aiohttp_client, tmp_path):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        # Patch ASHLR_DIR to avoid writing to real config
        with patch.object(_server_mod, "ASHLR_DIR", tmp_path):
            (tmp_path / "ashlr.yaml").write_text("{}")
            client = await aiohttp_client(app)
            payload = _make_pro_payload()
            token = _sign_license(payload)
            resp = await client.post("/api/license/activate",
                                     json={"license_key": token})
            assert resp.status == 200
            body = await resp.json()
            assert body["license"]["tier"] == "pro"
            assert body["license"]["is_pro"] is True
            # Verify app state was updated
            assert app["license"].is_pro is True

    @pytest.mark.asyncio
    async def test_activate_empty_key(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.post("/api/license/activate", json={"license_key": ""})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_activate_invalid_key(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.post("/api/license/activate", json={"license_key": "garbage"})
        assert resp.status == 400
        body = await resp.json()
        assert "error" in body

    @pytest.mark.asyncio
    @patch.object(_licensing_mod, "LICENSE_PUBLIC_KEY_PEM", TEST_PUB_PEM)
    async def test_activate_expired_key(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        payload = _make_expired_payload()
        token = _sign_license(payload)
        resp = await client.post("/api/license/activate", json={"license_key": token})
        assert resp.status == 400


class TestDeactivateLicenseEndpoint:
    @pytest.mark.asyncio
    async def test_deactivate_reverts_to_community(self, aiohttp_client, tmp_path):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        app = _make_test_app(license=pro)
        with patch.object(_server_mod, "ASHLR_DIR", tmp_path):
            (tmp_path / "ashlr.yaml").write_text("{}")
            client = await aiohttp_client(app)
            resp = await client.delete("/api/license/deactivate")
            assert resp.status == 200
            body = await resp.json()
            assert body["license"]["tier"] == "community"
            assert app["license"].is_pro is False


# ─────────────────────────────────────────────
# Feature Gating HTTP Tests
# ─────────────────────────────────────────────

class TestFeatureGatingEndpoints:
    @pytest.mark.asyncio
    async def test_intelligence_command_gated_on_community(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.post("/api/intelligence/command",
                                 json={"transcript": "spawn 3 agents"})
        assert resp.status == 403
        body = await resp.json()
        assert body["feature"] == "intelligence"

    @pytest.mark.asyncio
    async def test_intelligence_insights_gated_on_community(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.get("/api/intelligence/insights")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_create_workflow_gated_on_community(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.post("/api/workflows",
                                 json={"name": "test", "agents": [{"role": "backend", "task": "x"}]})
        assert resp.status == 403
        body = await resp.json()
        assert body["feature"] == "workflows"

    @pytest.mark.asyncio
    async def test_batch_spawn_gated_on_community(self, aiohttp_client):
        app = _make_test_app(license=COMMUNITY_LICENSE)
        client = await aiohttp_client(app)
        resp = await client.post("/api/agents/batch-spawn",
                                 json={"agents": [{"role": "backend", "task": "x"}]})
        assert resp.status == 403
        body = await resp.json()
        assert body["feature"] == "fleet_presets"

    @pytest.mark.asyncio
    async def test_pro_allows_intelligence(self, aiohttp_client):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        app = _make_test_app(license=pro)
        client = await aiohttp_client(app)
        resp = await client.get("/api/intelligence/insights")
        assert resp.status == 200  # Pro has access (may return empty)

    @pytest.mark.asyncio
    async def test_pro_allows_create_workflow(self, aiohttp_client):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        pro = License(tier="pro", max_agents=100, expires_at=future, features=PRO_FEATURES)
        app = _make_test_app(license=pro)
        client = await aiohttp_client(app)
        resp = await client.post("/api/workflows",
                                 json={"name": "test", "agents": [{"role": "backend", "task": "x"}]})
        # Should pass feature gate (may fail on other validation, but not 403)
        assert resp.status != 403


class TestAgentLimitEnforcement:
    @pytest.mark.asyncio
    async def test_community_spawn_limit(self, aiohttp_client):
        """Community plan should limit to 5 agents."""
        app = _make_test_app(license=COMMUNITY_LICENSE)
        manager = app["agent_manager"]
        # Fill up to limit with mock agents
        from collections import deque
        for i in range(5):
            agent = ashlr_server.Agent(
                id=f"a{i}", name=f"agent-{i}", role="general",
                status="working", working_dir=TEST_WORKING_DIR,
                backend="claude-code", task="test",
            )
            agent.output_lines = deque(maxlen=2000)
            manager.agents[agent.id] = agent

        client = await aiohttp_client(app)
        resp = await client.post("/api/agents", json={
            "role": "general",
            "task": "one more",
            "working_dir": TEST_WORKING_DIR,
        })
        # spawn() raises ValueError caught as 400 by the handler
        assert resp.status == 400
        body = await resp.json()
        assert "Maximum agents" in body["error"]
        assert "upgrade to Pro" in body["error"]


class TestAdminOnlyConfig:
    @pytest.mark.asyncio
    async def test_max_agents_clamped_to_license(self, aiohttp_client):
        """PUT /api/config should clamp max_agents to license ceiling."""
        app = _make_test_app(license=COMMUNITY_LICENSE)  # Community = 5 max, no auth
        client = await aiohttp_client(app)
        resp = await client.put("/api/config", json={"max_agents": 50})
        # It should accept but clamp — check the config
        if resp.status == 200:
            assert app["config"].max_agents <= 5

    @pytest.mark.asyncio
    async def test_admin_gate_unit(self):
        """The put_config handler checks admin when require_auth is True."""
        # Unit test: simulate request with non-admin user
        from ashlr_server import User
        req = MagicMock()
        config = Config(require_auth=True)
        req.app = {
            "config": config,
            "license": COMMUNITY_LICENSE,
            "ws_hub": MagicMock(broadcast=AsyncMock()),
        }
        non_admin = User(id="u1", email="u@t.com", display_name="U",
                         password_hash="x", role="member", org_id="o1")
        req.get = lambda k: non_admin if k == "user" else None
        req.json = AsyncMock(return_value={"max_agents": 10})

        resp = await ashlr_server.put_config(req)
        assert resp.status == 403
