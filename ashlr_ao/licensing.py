"""
Ashlr AO — Licensing

Ed25519-signed JWT license validation. Offline-first, no phone-home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import jwt

from ashlr_ao.constants import log

if TYPE_CHECKING:
    from aiohttp import web

    from ashlr_ao.config import Config


PRO_FEATURES: frozenset[str] = frozenset({
    "multi_user", "intelligence", "workflows", "fleet_presets", "unlimited_agents",
})

LICENSE_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEABZK/hIk2v95CNsJJ1tXRzZHIA7XUinjXqr17PoF8p9A=
-----END PUBLIC KEY-----"""


@dataclass
class License:
    """Represents a validated license for an Ashlr instance."""
    tier: str = "community"
    max_agents: int = 5
    max_seats: int = 1
    features: frozenset[str] = field(default_factory=frozenset)
    org_id: str = ""
    issued_at: str = ""
    expires_at: str = ""
    raw_key: str = ""

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return self.tier != "community"
        try:
            exp = datetime.fromisoformat(self.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > exp
        except Exception:
            return True

    @property
    def is_pro(self) -> bool:
        return self.tier == "pro" and not self.is_expired

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "max_agents": self.max_agents,
            "max_seats": self.max_seats,
            "features": sorted(self.features),
            "org_id": self.org_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "is_pro": self.is_pro,
            "is_expired": self.is_expired,
        }


COMMUNITY_LICENSE = License()


def validate_license(key: str) -> License:
    """Decode and verify an Ed25519-signed JWT license key.

    Returns a License on success; COMMUNITY_LICENSE on any failure.
    """
    if not key or not isinstance(key, str):
        return COMMUNITY_LICENSE
    key = key.strip()
    try:
        payload = jwt.decode(
            key,
            LICENSE_PUBLIC_KEY_PEM,
            algorithms=["EdDSA"],
            issuer="ashlr-licensing",
            options={"require": ["exp", "iss", "sub"]},
        )
        tier = payload.get("tier", "community")
        exp_ts = payload.get("exp", 0)
        iat_ts = payload.get("iat", 0)
        expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat() if exp_ts else ""
        issued_at = datetime.fromtimestamp(iat_ts, tz=timezone.utc).isoformat() if iat_ts else ""
        features = frozenset(payload.get("features", []))
        return License(
            tier=tier,
            max_agents=int(payload.get("max_agents", 100)),
            max_seats=int(payload.get("max_seats", 50)),
            features=features if features else (PRO_FEATURES if tier == "pro" else frozenset()),
            org_id=payload.get("sub", ""),
            issued_at=issued_at,
            expires_at=expires_at,
            raw_key=key,
        )
    except jwt.ExpiredSignatureError:
        log.warning("License key expired")
        return COMMUNITY_LICENSE
    except Exception as e:
        log.warning(f"License validation failed: {e}")
        return COMMUNITY_LICENSE


# ─────────────────────────────────────────────
# License Enforcement Helpers
# ─────────────────────────────────────────────

def _effective_max_agents(app: web.Application) -> int:
    """Return the effective max agents, clamped to license limit."""
    from ashlr_ao.config import Config as _Config  # runtime import

    config: _Config = app["config"]
    lic: License = app.get("license", COMMUNITY_LICENSE)
    if lic.is_pro:
        return min(config.max_agents, lic.max_agents)
    return min(config.max_agents, COMMUNITY_LICENSE.max_agents)


def _check_feature(request: web.Request, feature: str) -> web.Response | None:
    """Return a 403 response if the feature is gated on the current plan, else None."""
    from aiohttp import web as _web  # runtime import

    lic: License = request.app.get("license", COMMUNITY_LICENSE)
    if lic.is_pro:
        return None  # Pro has all features
    if feature not in PRO_FEATURES:
        return None  # Not a gated feature
    return _web.json_response({
        "error": f"The '{feature}' feature requires a Pro license. Upgrade at https://ashlr.dev/pro",
        "feature": feature,
        "current_plan": lic.tier,
    }, status=403)
