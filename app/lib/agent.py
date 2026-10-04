"""The cross-account STR agent, from the case page.

RC_STR_AGENT lives in a separate Snowflake account from the data. The app
calls the bridge pipeline in risk_copilot.agentic, which holds one key-pair
connection to each account (bridge/.env) and returns the reviewed draft and
its PDF. Only available where that package and the keys are present: a local
`riskcopilot app run`. In Streamlit in Snowflake the button explains why not.
"""

from __future__ import annotations

from typing import Any, Callable


def available() -> tuple[bool, str]:
    try:
        from risk_copilot.agentic.connections import agent_configured
    except Exception as exc:  # SiS / Community Cloud: the package is not uploaded
        return False, f"The agent bridge is not installed in this runtime ({type(exc).__name__})."
    if not agent_configured():
        return False, "No agent-account key found (bridge/keys/rc_agent_svc.p8 or AGENT_PRIVATE_KEY_PATH)."
    return True, ""


def generate(case_id: str, progress: Callable[[str, str], None], user: str) -> dict[str, Any]:
    from risk_copilot.agentic.str_pipeline import generate_ai_str

    return generate_ai_str(case_id, progress=progress, requested_by=f"app:{user}", channel="streamlit")
