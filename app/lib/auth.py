"""Demo sign-in: two personas, so maker-checker is visible in the UI.

This is a demonstration of the security model, not the security model itself.
The credentials are shown on the sign-in page on purpose, so a judge is never
blocked. What a real deployment does instead, and what holds regardless of
this page, is listed in SECURITY_NOTES.

The control that matters is in the database: ATTEST_STR refuses unless the
review approved this exact content hash, whoever is signed in here.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import streamlit as st

#: username -> persona. Passwords are compared as SHA-256 digests, in constant time.
PERSONAS: dict[str, dict[str, Any]] = {
    "analyst": {
        "name": "Priya Nair",
        "title": "AML Analyst",
        "duty": "Investigates, drafts and reviews STRs. Cannot sign.",
        "icon": ":material/manage_search:",
        "can_investigate": True,
        "can_attest": False,
        "sha256": "",
    },
    "officer": {
        "name": "Rajesh Menon",
        "title": "Principal Officer",
        "duty": "Signs or returns approved STRs. Does not draft them.",
        "icon": ":material/verified:",
        "can_investigate": False,
        "can_attest": True,
        "sha256": "",
    },
}

#: Shown on the sign-in page. Demo only -- see SECURITY_NOTES.
DEMO_PASSWORDS = {"analyst": "Analyst@2026", "officer": "Officer@2026"}

SECURITY_NOTES = [
    "**Maker-checker.** The analyst who drafts an STR cannot sign it; only the Principal "
    "Officer can (PML Rules, Rule 7). The app hides the other role's controls.",
    "**Enforced in the database too.** `ATTEST_STR` refuses unless the Reviewer approved "
    "this exact version and its SHA-256 still matches. Filing is impossible from any screen "
    "without a matching, hash-bound attestation.",
    "**Least privilege.** The hosted app connects as a dedicated service user that can read "
    "the demo data and run the case tools, and nothing else. It never sees the answer key.",
    "**Every action is logged** to `AUDIT.AGENT_ACTION_LOG`, including refusals and resets.",
    "**In production** this page is replaced by the bank's SSO (Snowflake SAML or OAuth), and "
    "each officer acts under their own Snowflake role (restricted caller's rights).",
]

_SESSION_KEY = "auth_user"


def _digest(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# The digests are derived once from the demo passwords, so the two can never drift.
for _user, _password in DEMO_PASSWORDS.items():
    PERSONAS[_user]["sha256"] = _digest(_password)


def sign_in(username: str, password: str) -> bool:
    user = PERSONAS.get(username.strip().lower())
    if not user or not hmac.compare_digest(_digest(password), user["sha256"]):
        return False
    st.session_state[_SESSION_KEY] = username.strip().lower()
    return True


def sign_out(prefill: str | None = None) -> None:
    """Sign out; optionally pre-fill the sign-in form for another persona."""
    st.session_state.pop(_SESSION_KEY, None)
    if prefill:
        fill(prefill)


def fill(username: str) -> None:
    """Put a persona's demo credentials into the sign-in form (an on_click callback)."""
    st.session_state["login_user"] = username
    st.session_state["login_password"] = DEMO_PASSWORDS[username]


def current() -> dict[str, Any] | None:
    key = st.session_state.get(_SESSION_KEY)
    return {"username": key, **PERSONAS[key]} if key in PERSONAS else None


def can(permission: str) -> bool:
    user = current()
    return bool(user and user.get(permission))
