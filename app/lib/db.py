"""Data access for the app: one session, parameterised queries, config-driven names.

Runs in two places with one code path:

* Streamlit in Snowflake -- ``st.connection("snowflake")`` gives the app owner's
  session. The ``risk_copilot`` package is not uploaded, so its import fails.
* Locally (``riskcopilot app run``) -- the package is installed, so the session
  comes from the same key-pair connection every other command uses.

Identifiers come from ``app_config.json`` (rendered from conf/, validated
there); values are always bound, never interpolated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

CFG: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "app_config.json").read_text("utf-8")
)
S: dict[str, str] = CFG["schema"]
DT: dict[str, str] = CFG["dt"]
VIEW: dict[str, str] = CFG["view"]
PROC: dict[str, str] = CFG["procedure"]
HERO: dict[str, Any] | None = CFG.get("hero")


@st.cache_resource(show_spinner=False)
def session() -> Any:
    try:
        from risk_copilot.config import load_settings
        from risk_copilot.session import create_session
    except ImportError:  # Streamlit in Snowflake
        return st.connection("snowflake").session()
    return create_session(load_settings(), unattended=True)


def query(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Run one statement; columns lower-cased."""
    df = session().sql(sql, params=params or []).to_pandas()
    df.columns = [c.lower() for c in df.columns]
    return df


@st.cache_data(ttl=10, show_spinner=False)
def cached(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    """``query`` with a short TTL: pages re-run on every click."""
    return query(sql, list(params))


def call(proc: str, *args: Any) -> Any:
    """CALL a procedure; returns its result (parsed if JSON) and drops cached reads."""
    marks = ", ".join("?" for _ in args)
    rows = session().sql(f"CALL {proc}({marks})", params=list(args)).collect()
    st.cache_data.clear()
    value = rows[0][0] if rows else None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def reason(exc: Exception) -> str:
    """The procedure's own message from a Snowflake error, without the envelope."""
    text = str(exc)
    marker = text.rfind(" : ")
    return text[marker + 3 :].strip() if marker != -1 else text.strip()


def variant(value: Any) -> Any:
    """A VARIANT/OBJECT/ARRAY column as Python (pandas gives JSON text)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def inr(value: Any) -> str:
    """Indian digit grouping: 9402691.42 -> '₹94,02,691'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    n = round(float(value))
    sign, digits = ("-" if n < 0 else ""), str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}"


def lakh(value: Any) -> str:
    """9402691.42 -> '₹94.03 lakh'."""
    if value is None:
        return "-"
    return f"₹{float(value) / 100000:,.2f} lakh"
