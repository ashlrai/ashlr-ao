"""
Ashlr AO — Authentication

Session-based auth middleware, ownership checks, and auth API handlers.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import uuid
from typing import TYPE_CHECKING

import bcrypt
from aiohttp import web

from ashlr_ao.licensing import _check_feature
from ashlr_ao.middleware import _get_client_ip

if TYPE_CHECKING:
    from ashlr_ao.config import Config
    from ashlr_ao.database import Database

log = logging.getLogger("ashlr")


def _check_agent_ownership(request: web.Request, agent) -> web.Response | None:
    """Check if current user can control this agent. Returns error response or None if allowed."""
    user = request.get("user")
    if not user:
        return None  # No auth — allow (require_auth is false)
    # Admin can control any agent
    if user.role == "admin":
        return None
    # Owner can control their own agent
    if agent.owner_id and agent.owner_id != user.id:
        return web.json_response(
            {"error": "Only the agent owner or an admin can perform this action"}, status=403
        )
    return None


_AUTH_PUBLIC_ROUTES = frozenset({
    "/", "/logo.png", "/api/health",
    "/api/auth/login", "/api/auth/register", "/api/auth/verify", "/api/auth/status",
    "/api/license/status",
})


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    """Session-based auth middleware with bearer token fallback."""
    config: Config = request.app["config"]
    if not config.require_auth:
        return await handler(request)

    path = request.path
    if path in _AUTH_PUBLIC_ROUTES:
        return await handler(request)

    # WebSocket: validate session from cookie (sent automatically on same-origin)
    if path == "/ws":
        session_id = _extract_session_cookie(request)
        if session_id:
            db: Database = request.app["db"]
            sess = await db.get_session(session_id)
            if sess:
                user = await db.get_user_by_id(sess["user_id"])
                if user:
                    request["user"] = user
                    return await handler(request)
        # Fallback: bearer token for backward compat
        token = request.query.get("token", "")
        if token and config.auth_token and hmac.compare_digest(token, config.auth_token):
            return await handler(request)
        return web.json_response({"error": "Unauthorized"}, status=401)

    # Session cookie check
    session_id = _extract_session_cookie(request)
    if session_id:
        db: Database = request.app["db"]
        sess = await db.get_session(session_id)
        if sess:
            user = await db.get_user_by_id(sess["user_id"])
            if user:
                request["user"] = user
                return await handler(request)

    # Bearer token fallback (API/CLI access)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if config.auth_token and hmac.compare_digest(token, config.auth_token):
            return await handler(request)

    return web.json_response({"error": "Unauthorized"}, status=401)


def _extract_session_cookie(request: web.Request) -> str:
    """Extract ashlr_session cookie value from request."""
    cookie = request.cookies.get("ashlr_session", "")
    return cookie if cookie and len(cookie) >= 32 else ""


def _make_slug(name: str) -> str:
    """Convert org name to URL-safe slug."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')
    return slug or "org"


def _set_session_cookie(response: web.Response, session_id: str, request: web.Request | None = None) -> None:
    """Set session cookie with appropriate security flags."""
    cookie_opts = {
        "httponly": True,
        "samesite": "Strict",
        "max_age": 86400,
        "path": "/",
    }
    # Use Secure flag if behind HTTPS reverse proxy
    if request and request.headers.get("X-Forwarded-Proto") == "https":
        cookie_opts["secure"] = True
    response.set_cookie("ashlr_session", session_id, **cookie_opts)


async def auth_status(request: web.Request) -> web.Response:
    """GET /api/auth/status — check if auth is required and if user is logged in."""
    config: Config = request.app["config"]
    db: Database = request.app["db"]

    if not config.require_auth:
        return web.json_response({"auth_required": False})

    user_count = await db.user_count()
    result = {"auth_required": True, "needs_setup": user_count == 0}

    # Check if current request has valid session
    session_id = _extract_session_cookie(request)
    if session_id:
        sess = await db.get_session(session_id)
        if sess:
            user = await db.get_user_by_id(sess["user_id"])
            if user:
                result["user"] = user.to_dict()

    return web.json_response(result)


async def auth_register(request: web.Request) -> web.Response:
    """POST /api/auth/register — register a new user. First user becomes admin + creates org."""
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip()
    org_name = str(data.get("org_name", "")).strip()

    if not email or "@" not in email:
        return web.json_response({"error": "Valid email required"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Password must be at least 8 characters"}, status=400)
    if not display_name:
        return web.json_response({"error": "Display name required"}, status=400)

    # Check if email already taken
    existing = await db.get_user_by_email(email)
    if existing:
        log.warning(f"Rejected registration: duplicate email {email} from {_get_client_ip(request)}")
        return web.json_response({"error": "Email already registered"}, status=409)

    user_count = await db.user_count()
    pw_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())

    if user_count == 0:
        # First user: create org + admin
        if not org_name:
            org_name = "My Team"
        org = await db.create_org(org_name, _make_slug(org_name))
        user = await db.create_user(email, display_name, pw_hash, role="admin", org_id=org.id)
        log.info(f"First user registered: {email} (admin) in org '{org_name}'")
    else:
        # Subsequent users must be invited (have a pre-created account with temp password)
        # For now, allow open registration if auth is enabled but check for invite
        return web.json_response({"error": "Registration requires an invite from an admin"}, status=403)

    # Auto-login after registration
    session_id = await db.create_session(user.id)
    await db.update_user_login(user.id)

    resp = web.json_response({"user": user.to_dict(), "org": org.to_dict()})
    _set_session_cookie(resp, session_id, request)
    return resp


async def auth_login(request: web.Request) -> web.Response:
    """POST /api/auth/login — authenticate with email/password."""
    db: Database = request.app["db"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    password = str(data.get("password", ""))

    if not email or not password:
        return web.json_response({"error": "Email and password required"}, status=400)

    ip = _get_client_ip(request)
    user = await db.get_user_by_email(email)
    if not user:
        log.warning(f"Failed login: unknown email {email} from {ip}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    if not await asyncio.to_thread(bcrypt.checkpw, password.encode(), user.password_hash.encode()):
        log.warning(f"Failed login: wrong password for {email} from {ip}")
        return web.json_response({"error": "Invalid credentials"}, status=401)

    session_id = await db.create_session(user.id)
    await db.update_user_login(user.id)
    log.info(f"Login: {email} from {ip}")

    org = await db.get_org(user.org_id) if user.org_id else None

    resp = web.json_response({
        "user": user.to_dict(),
        "org": org.to_dict() if org else None,
    })
    _set_session_cookie(resp, session_id, request)
    return resp


async def auth_logout(request: web.Request) -> web.Response:
    """POST /api/auth/logout — clear session."""
    db: Database = request.app["db"]
    session_id = _extract_session_cookie(request)
    if session_id:
        await db.delete_session(session_id)

    resp = web.json_response({"ok": True})
    resp.del_cookie("ashlr_session", path="/")
    return resp


async def auth_me(request: web.Request) -> web.Response:
    """GET /api/auth/me — return current user info from session."""
    db: Database = request.app["db"]

    session_id = _extract_session_cookie(request)
    if not session_id:
        return web.json_response({"error": "Not authenticated"}, status=401)

    sess = await db.get_session(session_id)
    if not sess:
        return web.json_response({"error": "Session expired"}, status=401)

    user = await db.get_user_by_id(sess["user_id"])
    if not user:
        return web.json_response({"error": "User not found"}, status=401)

    org = await db.get_org(user.org_id) if user.org_id else None
    return web.json_response({
        "user": user.to_dict(),
        "org": org.to_dict() if org else None,
    })


async def auth_invite(request: web.Request) -> web.Response:
    """POST /api/auth/invite — admin-only: create a new user with temp password."""
    if r := _check_feature(request, "multi_user"):
        return r
    db: Database = request.app["db"]

    # Must be authenticated as admin
    user = request.get("user")
    if not user or user.role != "admin":
        return web.json_response({"error": "Admin access required"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    email = str(data.get("email", "")).lower().strip()
    display_name = str(data.get("display_name", "")).strip()

    if not email or "@" not in email:
        return web.json_response({"error": "Valid email required"}, status=400)
    if not display_name:
        display_name = email.split("@")[0]

    existing = await db.get_user_by_email(email)
    if existing:
        return web.json_response({"error": "Email already registered"}, status=409)

    # Generate temp password
    temp_password = uuid.uuid4().hex[:12]
    pw_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(temp_password.encode(), bcrypt.gensalt()).decode())

    invited_user = await db.create_user(email, display_name, pw_hash, role="member", org_id=user.org_id)
    log.info(f"User invited: {email} by {user.email}")

    return web.json_response({
        "user": invited_user.to_dict(),
        "temp_password": temp_password,
    })


async def auth_team(request: web.Request) -> web.Response:
    """GET /api/auth/team — list all users in the current user's org."""
    if r := _check_feature(request, "multi_user"):
        return r
    db: Database = request.app["db"]

    user = request.get("user")
    if not user:
        return web.json_response({"error": "Not authenticated"}, status=401)

    users = await db.get_org_users(user.org_id)
    return web.json_response({
        "users": [u.to_dict() for u in users],
    })


async def verify_auth(request: web.Request) -> web.Response:
    """POST /api/auth/verify — validate an auth token (backward compat)."""
    config: Config = request.app["config"]
    if not config.require_auth:
        return web.json_response({"valid": True, "auth_required": False})

    try:
        data = await request.json()
    except Exception:
        data = {}
    token = data.get("token", "")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    valid = bool(token and config.auth_token and hmac.compare_digest(token, config.auth_token))
    return web.json_response({"valid": valid, "auth_required": True})

