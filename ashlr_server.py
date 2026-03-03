"""Backward-compatibility shim.

Re-exports everything from ashlr_ao.server so that existing imports
(``from ashlr_server import …`` and ``import ashlr_server``) keep working.
"""

from ashlr_ao.server import *  # noqa: F401,F403

# Private names used by tests — not covered by wildcard import.
from ashlr_ao.server import (  # noqa: F401
    _strip_ansi,
    _check_agent_ownership,
    _check_feature,
    _effective_max_agents,
    _make_slug,
    _extract_session_cookie,
    _suggest_followup,
    _keyword_parse_command,
    _extract_question,
    _resolve_agent_refs,
    _alert_throttle,
    _get_rate_tier,
    _RATE_LIMIT_TIERS,
    _validate_workflow_specs,
    _SECRET_PATTERNS,
    _ANSI_ESCAPE_RE,
    _supervised_task,
)

if __name__ == "__main__":
    from ashlr_ao.server import main
    main()
