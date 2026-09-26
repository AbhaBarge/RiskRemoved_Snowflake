"""Empirical Snowflake capability probe.

Every claim about what an account can do is tested with the smallest possible
real statement, because a name in the documentation is not availability and one
function's failure does not imply another's. Failures are classified, since the
classes have different consequences:

  a  absent      - object/function does not exist (wrong name, or not in region)
  b  edition     - "requires Enterprise Edition or higher"
  c  trial       - 399258 or similar trial-account gate
  d  privilege   - fixable by granting something
  e  precondition- Anaconda terms, missing data, no warehouse
  f  unclassified- recorded verbatim rather than guessed at

Output is ``conf/generated/capabilities.<connection>.yml``, which ``doctor``
reads so the build disables unavailable features instead of failing at runtime.

The probe database is created, used and dropped inside one run. It is named
distinctly from anything the build owns and the drop is verified, because a
capability probe that leaves objects behind is a liability.

Usage:
    python scripts/probe_capabilities.py [--profile dev] [--keep-db]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from risk_copilot.config import Settings
from risk_copilot.session import create_session

PROBE_DB = "RC_CAPABILITY_PROBE"
PROBE_SCHEMA = f"{PROBE_DB}.PROBE"

#: Probes run as ACCOUNTADMIN deliberately. The question being answered is "is
#: this possible on this account at all", so the probe must not be limited by a
#: build role's grants -- otherwise a missing GRANT (class d) is indistinguishable
#: from an edition gate (class b). The build role's own grants are a separate
#: concern, checked by ``doctor``.
PROBE_ROLE = "ACCOUNTADMIN"

# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #

FailureClass = str

_CODE_RE = re.compile(r"\b(\d{6})\b")


def _error_code(text: str) -> str | None:
    """Return the first six-digit Snowflake error code in ``text``."""
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


def classify(text: str) -> FailureClass:
    """Map an error message to one of the classes documented in the module docstring."""
    lowered = text.lower()

    # Trial gates are checked before edition gates: the trial message sometimes
    # also mentions an edition, and the trial gate is the one that binds.
    if "399258" in text or "trial account" in lowered:
        return "c_trial"
    # The REST inference endpoints answer 403/003001 "This account is not allowed
    # to access this endpoint" -- no mention of a trial. Binned with the trial gate
    # because every SQL equivalent of the same capability returns the explicit
    # trial message on this account. That is an inference, and it is recorded here
    # rather than hidden.
    if "not allowed to access this endpoint" in lowered:
        return "c_trial"
    if "enterprise edition" in lowered or "business critical" in lowered:
        return "b_edition"
    # Standard edition caps Time Travel at one day. The message names neither an
    # edition nor a feature, but the cause is the edition, so bin it there
    # rather than leaving the single most consequential retention fact unclassified.
    if "exceeds maximum allowable retention time" in lowered:
        return "b_edition"
    if "anaconda" in lowered or "terms of service" in lowered or "terms of use" in lowered:
        return "e_precondition"
    if "no module named" in lowered:
        return "e_precondition"
    # Disabled by an account parameter rather than absent. Recorded separately
    # because it *looks* fixable -- though on this account even ACCOUNTADMIN is
    # refused the ALTER ACCOUNT that would enable it.
    if "is disabled. set" in lowered:
        return "e_precondition"
    # A recipient that is not a user of this account, or whose address is not
    # verified, is a setup condition -- SYSTEM$SEND_EMAIL itself works.
    if "not yet validated" in lowered or "are not allowed" in lowered:
        return "e_precondition"
    if "insufficient privileges" in lowered or "not authorized to perform" in lowered:
        return "d_privilege"
    if (
        "unsupported feature" in lowered
        or "unknown function" in lowered
        or "unknown user-defined table function" in lowered
        or "unknown user-defined function" in lowered
        or "invalid identifier" in lowered
        or "does not exist or not authorized" in lowered
        or "is not supported for" in lowered
        or "is not recognized" in lowered
        # SNOWFLAKE.ML.CLASSIFICATION reports this on an account where
        # ANOMALY_DETECTION and FORECAST both succeed, so it is not an edition or
        # trial gate: no CLASSIFICATION model version is served in this region.
        or "must have an active version defined" in lowered
    ):
        return "a_absent"
    return "f_unclassified"


# --------------------------------------------------------------------------- #
# Probe definition
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Probe:
    """One capability test.

    Args:
        id: Stable key written to the capabilities file.
        group: Reporting group, aligned with the probe matrix.
        statements: Run in order; all must succeed for the probe to pass. The
            last one is recorded as *the* statement, since it is the one that
            expresses the capability -- the earlier ones are fixtures.
        cleanup: Best-effort teardown, failures ignored.
        why: What breaks in the build if this is unavailable.
        runner: Alternative to ``statements`` for probes needing client-side
            work (staging a file, importing a Python package).
    """

    id: str
    group: str
    why: str
    statements: Sequence[str] = ()
    cleanup: Sequence[str] = ()
    runner: Callable[[Any, Settings], str] | None = None
    #: When true, the question is only "does this entry point exist on this
    #: account". Any failure other than ``a_absent``/``b_edition``/``c_trial`` is
    #: then treated as present-but-not-exercised, and the reason is recorded in
    #: ``note``. Used where a full end-to-end call needs fixtures far larger than
    #: a probe should create.
    resolution_only: bool = False


@dataclass(slots=True)
class Result:
    id: str
    group: str
    why: str
    available: bool
    statement: str
    failure_class: str | None = None
    error_code: str | None = None
    error: str | None = None
    note: str | None = None


# --------------------------------------------------------------------------- #
# Model roster. Newest first, taken from the regional-availability doc page and
# widened with plausible current names; the account decides which are real.
# --------------------------------------------------------------------------- #

CLAUDE_MODELS = [
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-4-sonnet",
    "claude-3-7-sonnet",
]
OPENAI_MODELS = [
    "openai-gpt-5.4-mini",
    "openai-gpt-5.1",
    "openai-gpt-5",
    "openai-gpt-5-mini",
    "openai-gpt-5-nano",
    "openai-gpt-4.1",
]
GOOGLE_MODELS = ["gemini-3.1-pro"]
LLAMA_MODELS = [
    "llama4-maverick",
    "llama3.3-70b",
    "llama3.1-405b",
    "llama3.1-70b",
    "llama3.1-8b",
    "llama3.2-3b",
]
MISTRAL_MODELS = ["mistral-large3", "mistral-large2", "mixtral-8x7b", "mistral-7b"]
OTHER_MODELS = ["qwen3-32b", "snowflake-arctic", "snowflake-llama-3.3-70b"]

ALL_MODELS = (
    CLAUDE_MODELS + OPENAI_MODELS + GOOGLE_MODELS + LLAMA_MODELS + MISTRAL_MODELS + OTHER_MODELS
)

#: Family per model, so the reviewer-differs-from-drafter constraint in
#: conf/models.yml can be enforced from evidence rather than by eye.
MODEL_FAMILY = {
    **dict.fromkeys(CLAUDE_MODELS, "anthropic"),
    **dict.fromkeys(OPENAI_MODELS, "openai"),
    **dict.fromkeys(GOOGLE_MODELS, "google"),
    **dict.fromkeys(LLAMA_MODELS, "meta"),
    **dict.fromkeys(MISTRAL_MODELS, "mistral"),
    "qwen3-32b": "qwen",
    "snowflake-arctic": "snowflake",
    "snowflake-llama-3.3-70b": "snowflake",
}


# --------------------------------------------------------------------------- #
# Client-side runners
# --------------------------------------------------------------------------- #


def _run_parse_document(session: Any, _settings: Settings) -> str:
    """Parse the file staged by :func:`stage_fixture`.

    AI_PARSE_DOCUMENT takes a FILE, not a stage reference plus a name -- passing
    ``@stage, 'name'`` fails with an argument error that reads like absence.
    """
    sql = (
        f"SELECT AI_PARSE_DOCUMENT(TO_FILE('@{PROBE_SCHEMA}.PROBE_STAGE', 'probe.txt'), "
        "{'mode': 'LAYOUT'})"
    )
    session.sql(sql).collect()
    return sql


def stage_fixture(session: Any) -> None:
    """Create the probe stage and put the fixture files on it.

    Done during setup rather than inside a probe so that probe *ordering* can
    never turn a present capability into a false negative -- AI_PARSE_DOCUMENT,
    AI_TRANSCRIBE and EXECUTE_AI_EVALUATION all need a staged file to exist.
    """
    session.sql(f"CREATE OR REPLACE STAGE {PROBE_SCHEMA}.PROBE_STAGE").collect()
    files = {
        "probe.txt": "Clause 3.1 Report cash transactions above Rs 10,00,000.\n",
        # A real config file, so a failure means the evaluation capability is
        # missing rather than the file being absent.
        "agent_evaluation_config.yaml": (
            f"name: rc-probe-eval\nagent: {PROBE_SCHEMA}.PROBE_AGENT\nmetrics:\n  - groundedness\n"
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in files.items():
            local = Path(tmp) / name
            local.write_text(body, encoding="utf-8")
            session.file.put(
                str(local), f"@{PROBE_SCHEMA}.PROBE_STAGE", auto_compress=False, overwrite=True
            )


#: One user turn, reused across every agent entry point so the four paths differ
#: only in transport -- otherwise a payload difference could explain a difference
#: in outcome.
_AGENT_MESSAGES = [
    {"role": "user", "content": [{"type": "text", "text": "What is the total by account?"}]}
]


def _agent_json(payload: str) -> None:
    """Raise if an agent response body carries an error instead of an answer."""
    try:
        body = json.loads(payload)
    except (TypeError, ValueError):
        return
    if isinstance(body, dict) and (body.get("code") or body.get("error_code")):
        code = body.get("code") or body.get("error_code")
        raise RuntimeError(f"{code}: {body.get('message', payload[:300])}")


def _agent_run_sql_no_object(session: Any) -> str:
    """SNOWFLAKE.CORTEX.AGENT_RUN -- orchestration without an agent object."""
    request = json.dumps(
        {
            "models": {"orchestration": "claude-sonnet-4-6"},
            "messages": _AGENT_MESSAGES,
        }
    ).replace("'", "''")
    sql = f"SELECT SNOWFLAKE.CORTEX.AGENT_RUN('{request}')"
    _agent_json(str(session.sql(sql).collect()[0][0]))
    return "SELECT SNOWFLAKE.CORTEX.AGENT_RUN(<inline agent spec>)"


def _rest_context(settings: Settings) -> tuple[str, str]:
    """Return ``(base_url, bearer_token)`` for the REST surfaces.

    Several Cortex capabilities have no SQL entry point at all, and at least one
    (Analyst) is served even when the SQL inference path is blocked. Testing only
    SQL therefore under-reports what an account can do.
    """
    from snowflake.connector.auth import AuthByKeyPair

    from risk_copilot.session import _load_private_key, build_connect_params

    params = build_connect_params(settings)
    account = str(params["account"])
    key_bytes = _load_private_key(settings)
    if key_bytes is None:
        raise RuntimeError(
            "No key pair configured (RC_SNOWFLAKE_PRIVATE_KEY_PATH); cannot mint a "
            "JWT for the REST endpoints."
        )
    token = AuthByKeyPair(private_key=key_bytes).prepare(account=account, user=str(params["user"]))
    return f"https://{account}.snowflakecomputing.com", token


def _rest_post(
    settings: Settings,
    path: str,
    body: dict[str, Any],
    *,
    accept: str = "application/json",
    require: str | None = None,
) -> str:
    """POST to a Snowflake REST path, raising with the server's own words on failure.

    Args:
        require: If given, a 2xx response whose body does not contain this
            substring is treated as a failure. Needed because some endpoints
            return 200 while carrying an error in the payload -- a streaming
            agent run answers ``HTTP 200`` with ``event: error`` inside.
    """
    import requests

    base, token = _rest_context(settings)
    url = f"{base}{path}"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Content-Type": "application/json",
            "Accept": accept,
        },
        json=body,
        timeout=180,
    )
    flat = " ".join(response.text.split())
    if response.status_code >= 300:
        raise RuntimeError(f"HTTP {response.status_code}: {flat[:400]}")
    # A 2xx that carries an error payload is not a working capability.
    if '"code"' in flat and "error" in flat.lower():
        raise RuntimeError(f"HTTP {response.status_code} with error payload: {flat[:400]}")
    if require is not None and require not in flat:
        raise RuntimeError(f"HTTP {response.status_code} but no {require!r}: {flat[:400]}")
    return f"POST {url}"


def _run_analyst_rest(session: Any, settings: Settings) -> str:
    """Ask Cortex Analyst a natural-language question over the probe semantic view.

    Analyst is REST-only -- there is no SQL entry point. An earlier version of
    this probe used ``SNOWFLAKE.CORTEX.COMPLETE`` as a stand-in and concluded
    Analyst was blocked; that was an inference, and it was wrong. Analyst is
    served independently of the AI_COMPLETE inference path, so it has to be
    called for real.
    """
    import requests

    from risk_copilot.session import _load_private_key, build_connect_params

    params = build_connect_params(settings)
    account = str(params["account"])
    user = str(params["user"])

    key_bytes = _load_private_key(settings)
    if key_bytes is None:
        raise RuntimeError(
            "No key pair configured (RC_SNOWFLAKE_PRIVATE_KEY_PATH); cannot mint a "
            "JWT for the Analyst REST endpoint."
        )

    from snowflake.connector.auth import AuthByKeyPair

    token = AuthByKeyPair(private_key=key_bytes).prepare(account=account, user=user)
    url = f"https://{account}.snowflakecomputing.com/api/v2/cortex/analyst/message"
    statement = f"POST {url}  semantic_view={PROBE_SCHEMA}.PROBE_SV"

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "What is the total by account?"}],
                }
            ],
            "semantic_view": f"{PROBE_SCHEMA}.PROBE_SV",
        },
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {' '.join(response.text.split())[:400]}")

    body = response.json()
    # A 200 that carries no generated SQL is not a working text-to-SQL path.
    content = (body.get("message") or {}).get("content") or []
    if not any(part.get("type") == "sql" for part in content if isinstance(part, dict)):
        raise RuntimeError(f"200 but no SQL generated: {' '.join(response.text.split())[:400]}")
    return statement


def _run_data_agent_run(session: Any, _settings: Settings) -> str:
    """Invoke the probe agent and inspect the returned payload.

    DATA_AGENT_RUN does not raise when access is denied -- it returns a JSON body
    carrying the error, so a statement that merely *succeeds* proves nothing. The
    payload has to be read, or a blocked agent layer records as available.
    """
    sql = (
        f"SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN('{PROBE_SCHEMA}.PROBE_AGENT', "
        '\'{"messages":[{"role":"user","content":'
        '[{"type":"text","text":"What is 2+2?"}]}]}\')'
    )
    payload = session.sql(sql).collect()[0][0]
    text = str(payload)
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        body = {}

    if isinstance(body, dict) and (body.get("code") or body.get("error_code")):
        code = body.get("code") or body.get("error_code")
        raise RuntimeError(f"{code}: {body.get('message', text[:300])}")
    return sql


def _run_model_registry(session: Any, _settings: Settings) -> str:
    """Log a trivial sklearn model to the Snowflake Model Registry.

    An sklearn estimator rather than a ``CustomModel``: this module uses
    ``from __future__ import annotations``, which stringifies the type hints the
    registry introspects on a custom handler, producing a signature error that
    looks like a capability failure but is an artefact of this file.
    """
    stmt = "snowflake.ml.registry.Registry(...).log_model(LinearRegression(), ...)"
    import pandas as pd
    from sklearn.linear_model import LinearRegression  # type: ignore[import-untyped]
    from snowflake.ml.registry import Registry  # type: ignore[import-untyped]

    frame = pd.DataFrame({"X": [1.0, 2.0, 3.0]})
    model = LinearRegression().fit(frame[["X"]], [2.0, 4.0, 6.0])
    registry = Registry(session=session, database_name=PROBE_DB, schema_name="PROBE")
    registry.log_model(
        model,
        model_name="RC_PROBE_MODEL",
        version_name="V1",
        sample_input_data=frame,
    )
    return stmt


def _run_feature_store(session: Any, _settings: Settings) -> str:
    """Register an Entity and a FeatureView."""
    stmt = "snowflake.ml.feature_store.FeatureStore(...).register_feature_view(...)"
    from snowflake.ml.feature_store import (  # type: ignore[import-untyped]
        CreationMode,
        Entity,
        FeatureStore,
        FeatureView,
    )

    store = FeatureStore(
        session=session,
        database=PROBE_DB,
        name="PROBE",
        default_warehouse=session.get_current_warehouse(),
        creation_mode=CreationMode.CREATE_IF_NOT_EXIST,
    )
    entity = Entity(name="PROBE_ACCT", join_keys=["ACCOUNT_ID"])
    store.register_entity(entity)
    frame = session.sql(f"SELECT ACCOUNT_ID, AMT FROM {PROBE_SCHEMA}.FS_SRC")
    view = FeatureView(name="PROBE_FV", entities=[entity], feature_df=frame, refresh_freq=None)
    store.register_feature_view(feature_view=view, version="V1")
    return stmt


# --------------------------------------------------------------------------- #
# The probe matrix
# --------------------------------------------------------------------------- #


def build_probes() -> list[Probe]:
    probes: list[Probe] = []

    # ------------------------------------------------- built-in string metrics #
    # Named in conf/ml.yml:preconditions.verify_functions, so the capabilities
    # file must answer for them too -- otherwise doctor cannot resolve the
    # fuzzy-sanctions feature from evidence.
    probes += [
        Probe(
            id="JAROWINKLER_SIMILARITY",
            group="string_metrics",
            why="Fuzzy sanctions screening (ml.yml tier1.fuzzy_sanctions)",
            statements=["SELECT JAROWINKLER_SIMILARITY('kumar', 'kumaar')"],
        ),
        Probe(
            id="EDITDISTANCE",
            group="string_metrics",
            why="Fuzzy sanctions screening, alternative metric",
            statements=["SELECT EDITDISTANCE('kumar', 'kumaar')"],
        ),
        Probe(
            id="SOUNDEX",
            group="string_metrics",
            why="Blocking key that keeps the sanctions cross join tractable",
            statements=["SELECT SOUNDEX('kumar')"],
        ),
    ]

    # ---------------------------------------------------------------- cortex #
    for model in ALL_MODELS:
        probes.append(
            Probe(
                id=f"AI_COMPLETE:{model}",
                group="cortex_models",
                why=f"Cortex model {model} ({MODEL_FAMILY[model]} family)",
                statements=[f"SELECT AI_COMPLETE('{model}', 'Reply with the single word ok')"],
            )
        )

    probes += [
        Probe(
            id="SNOWFLAKE.CORTEX.COMPLETE",
            group="cortex_functions",
            why="Older namespace; may be gated separately from AI_COMPLETE",
            statements=["SELECT SNOWFLAKE.CORTEX.COMPLETE('llama3.1-8b', 'ok')"],
        ),
        Probe(
            id="AI_COUNT_TOKENS",
            group="cortex_functions",
            why="Token budgeting before an agent turn (models.yml:limits)",
            statements=["SELECT AI_COUNT_TOKENS('llama3.1-8b', 'count these tokens')"],
        ),
        Probe(
            id="AI_SENTIMENT",
            group="cortex_functions",
            why="Case-comment tone; not on the critical path",
            statements=["SELECT AI_SENTIMENT('the customer was uncooperative')"],
        ),
        Probe(
            id="AI_CLASSIFY",
            group="cortex_functions",
            why="KYC narrative contradiction detection (ml.yml future_scope)",
            statements=["SELECT AI_CLASSIFY('wire to a shell company', ['suspicious', 'benign'])"],
        ),
        Probe(
            id="AI_FILTER",
            group="cortex_functions",
            why="Narrative-vs-record contradiction filter",
            statements=["SELECT AI_FILTER('is 2 greater than 1?')"],
        ),
        Probe(
            id="AI_AGG",
            group="cortex_functions",
            why="Cross-row narrative summarisation for an evidence bundle",
            statements=[
                "SELECT AI_AGG(txt, 'summarise') "
                "FROM (SELECT 'alpha' AS txt UNION ALL SELECT 'beta')"
            ],
        ),
        Probe(
            id="AI_SIMILARITY",
            group="cortex_functions",
            why="Embedding-based entity resolution alternative to Jaro-Winkler",
            statements=["SELECT AI_SIMILARITY('Mohammed Ali', 'Muhammad Ali')"],
        ),
        Probe(
            id="AI_EMBED",
            group="cortex_functions",
            why="Alias/transliteration resolution (ml.yml embedding_entity_resolution)",
            statements=["SELECT AI_EMBED('snowflake-arctic-embed-l-v2.0', 'kumar trading')"],
        ),
        Probe(
            id="AI_EXTRACT",
            group="cortex_functions",
            why="Policy clause extraction from the FIU circular",
            statements=[
                "SELECT AI_EXTRACT("
                "text => 'Report cash above Rs 10,00,000 within 7 days', "
                "responseFormat => {'threshold': 'the rupee threshold'})"
            ],
        ),
        Probe(
            id="AI_TRANSCRIBE",
            group="cortex_functions",
            why="Not used by the design; probed only to complete the matrix",
            statements=[
                f"SELECT AI_TRANSCRIBE(TO_FILE('@{PROBE_SCHEMA}.PROBE_STAGE', 'probe.txt'))"
            ],
        ),
        Probe(
            id="AI_PARSE_DOCUMENT",
            group="cortex_functions",
            why="Entire policy ingestion pipeline (diagrams 3, 10)",
            runner=_run_parse_document,
        ),
        Probe(
            id="CORTEX_SEARCH_SERVICE",
            group="cortex_services",
            why="REGULATORY_SEARCH_SVC, clause retrieval, citation grounding",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.SEARCH_SRC (ID NUMBER, BODY VARCHAR)",
                f"INSERT INTO {PROBE_SCHEMA}.SEARCH_SRC VALUES "
                "(1, 'report cash above ten lakh'), (2, 'file within seven days'), "
                "(3, 'retain records five years')",
                f"CREATE OR REPLACE CORTEX SEARCH SERVICE {PROBE_SCHEMA}.PROBE_SVC "
                "ON BODY WAREHOUSE = {warehouse} TARGET_LAG = '1 hour' "
                f"AS SELECT ID, BODY FROM {PROBE_SCHEMA}.SEARCH_SRC",
            ],
            cleanup=[f"DROP CORTEX SEARCH SERVICE IF EXISTS {PROBE_SCHEMA}.PROBE_SVC"],
        ),
        Probe(
            id="CREATE_SEMANTIC_VIEW",
            group="cortex_services",
            why="Semantic view OBJECT -- S3 claims this is creatable on Standard",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.SV_TXN "
                "(TXN_ID NUMBER, ACCOUNT_ID VARCHAR, AMT NUMBER(18,2))",
                f"INSERT INTO {PROBE_SCHEMA}.SV_TXN VALUES (1,'A',100.00),(2,'B',200.00)",
                f"CREATE OR REPLACE SEMANTIC VIEW {PROBE_SCHEMA}.PROBE_SV "
                f"TABLES (txn AS {PROBE_SCHEMA}.SV_TXN PRIMARY KEY (TXN_ID)) "
                "FACTS (txn.amt AS AMT) "
                "DIMENSIONS (txn.account AS ACCOUNT_ID) "
                "METRICS (txn.total AS SUM(txn.amt))",
            ],
        ),
        Probe(
            id="QUERY_SEMANTIC_VIEW",
            group="cortex_services",
            why="SQL-level querying of the semantic view; distinct from Analyst NL",
            statements=[f"SELECT * FROM SEMANTIC_VIEW({PROBE_SCHEMA}.PROBE_SV METRICS txn.total)"],
        ),
        Probe(
            id="CORTEX_ANALYST_REST",
            group="cortex_services",
            why="Natural-language querying of the semantic view (Suite 2)",
            runner=_run_analyst_rest,
        ),
        Probe(
            id="CREATE_AGENT",
            group="cortex_services",
            why="All four agent modes (diagrams 7, 13)",
            statements=[
                f"""CREATE OR REPLACE AGENT {PROBE_SCHEMA}.PROBE_AGENT
  COMMENT = 'capability probe'
  FROM SPECIFICATION
  $$
  models:
    orchestration: auto

  instructions:
    response: "Be concise."

  tools: []
  $$"""
            ],
            # Deliberately no cleanup: EXECUTE_AI_EVALUATION and DATA_AGENT_RUN
            # below both reference this agent. Dropped with the database.
        ),
        Probe(
            id="DATA_AGENT_RUN",
            group="cortex_services",
            why="Creating an agent OBJECT is not the same as running one; this is the real gate",
            runner=_run_data_agent_run,
        ),
        Probe(
            id="EXECUTE_AI_EVALUATION",
            group="cortex_services",
            why="LLM-as-judge groundedness metrics (Suite 3)",
            # STATUS on a run that does not exist is the smallest real call. What
            # is being tested is whether the function resolves at all; a
            # "no such run" error would still mean the capability is present, so
            # the failure class matters more than the pass/fail here.
            statements=[
                "CALL EXECUTE_AI_EVALUATION('STATUS', "
                "OBJECT_CONSTRUCT('run_name', 'rc-probe-run'), "
                f"'@{PROBE_SCHEMA}.PROBE_STAGE/agent_evaluation_config.yaml')"
            ],
            resolution_only=True,
        ),
    ]

    # ------------------------------------------------------------------------ #
    # Alternate entry points for capabilities the SQL surface refuses.
    #
    # Cortex Analyst proved that one blocked surface does not imply the feature is
    # blocked, so every remaining "unavailable" is re-tested against every other
    # documented path before the build accepts the loss.
    # ------------------------------------------------------------------------ #
    probes += [
        Probe(
            id="AGENT_RUN_SQL_NO_OBJECT",
            group="cortex_agent_paths",
            why="Agent orchestration without an agent object, via SQL",
            runner=lambda session, settings: _agent_run_sql_no_object(session),
        ),
        Probe(
            id="AGENT_RUN_REST_OBJECT",
            group="cortex_agent_paths",
            why="Agent Run REST against the agent object -- the documented preferred path",
            runner=lambda session, settings: _rest_post(
                settings,
                f"/api/v2/databases/{PROBE_DB}/schemas/PROBE/agents/PROBE_AGENT:run",
                {"messages": _AGENT_MESSAGES, "stream": False},
            ),
        ),
        Probe(
            id="AGENT_RUN_REST_OBJECT_STREAMING",
            group="cortex_agent_paths",
            why="The streaming variant answers HTTP 200 even when denied; needs its own probe",
            runner=lambda session, settings: _rest_post(
                settings,
                f"/api/v2/databases/{PROBE_DB}/schemas/PROBE/agents/PROBE_AGENT:run",
                {"messages": _AGENT_MESSAGES},
                accept="text/event-stream",
            ),
        ),
        Probe(
            id="AGENT_RUN_REST_INLINE",
            group="cortex_agent_paths",
            why="Agent Run REST with the configuration inline, no agent object",
            runner=lambda session, settings: _rest_post(
                settings,
                "/api/v2/cortex/agent:run",
                {
                    "messages": _AGENT_MESSAGES,
                    "stream": False,
                    "models": {"orchestration": "claude-sonnet-4-6"},
                },
            ),
        ),
        Probe(
            id="CORTEX_THREADS_REST",
            group="cortex_agent_paths",
            why="Conversation history for a multi-turn agent; distinct from running one",
            runner=lambda session, settings: _rest_post(
                settings, "/api/v2/cortex/threads", {"origin_application": "risk_copilot"}
            ),
        ),
        Probe(
            id="CORTEX_REST_CHAT_COMPLETIONS",
            group="cortex_inference_paths",
            why="OpenAI-spec Cortex REST API -- a second inference path independent of AI_COMPLETE",
            runner=lambda session, settings: _rest_post(
                settings,
                "/api/v2/cortex/v1/chat/completions",
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "Reply with the single word ok"}],
                },
            ),
        ),
        Probe(
            id="CORTEX_REST_MESSAGES",
            group="cortex_inference_paths",
            why="Anthropic-spec Cortex REST API -- a third inference path",
            runner=lambda session, settings: _rest_post(
                settings,
                "/api/v2/cortex/v1/messages",
                {
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "Reply with the single word ok"}],
                },
            ),
        ),
        Probe(
            id="CORTEX_INFERENCE_COMPLETE_REST",
            group="cortex_inference_paths",
            why="Cortex Inference REST endpoint -- a fourth inference path",
            runner=lambda session, settings: _rest_post(
                settings,
                "/api/v2/cortex/inference:complete",
                {
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "Reply with the single word ok"}],
                },
            ),
        ),
        Probe(
            id="AI_SUMMARIZE_AGG",
            group="cortex_functions",
            why="Aggregate summarisation. Works where AI_COMPLETE does not.",
            statements=["SELECT AI_SUMMARIZE_AGG(c) FROM (SELECT 'alpha beta gamma' AS c)"],
        ),
        Probe(
            id="EMBED_TEXT_768",
            group="cortex_functions",
            why="Underlies Cortex Search; probed directly to locate the real blocker",
            statements=[
                "SELECT ARRAY_SIZE(SNOWFLAKE.CORTEX.EMBED_TEXT_768("
                "'snowflake-arctic-embed-m', 'kumar trading')::ARRAY)"
            ],
        ),
        Probe(
            id="EMBED_TEXT_1024",
            group="cortex_functions",
            why="Higher-dimension embedding, used by arctic-embed-l-v2.0",
            statements=[
                "SELECT ARRAY_SIZE(SNOWFLAKE.CORTEX.EMBED_TEXT_1024("
                "'snowflake-arctic-embed-l-v2.0', 'kumar trading')::ARRAY)"
            ],
        ),
        Probe(
            id="CORTEX_SEARCH_EXPLICIT_EMBEDDING_MODEL",
            group="cortex_services",
            why="Whether naming an embedding model explicitly avoids the default's gate",
            statements=[
                f"CREATE OR REPLACE CORTEX SEARCH SERVICE {PROBE_SCHEMA}.PROBE_SVC2 "
                "ON BODY WAREHOUSE = {warehouse} TARGET_LAG = '1 hour' "
                "EMBEDDING_MODEL = 'snowflake-arctic-embed-l-v2.0' "
                f"AS SELECT ID, BODY FROM {PROBE_SCHEMA}.SEARCH_SRC"
            ],
            cleanup=[f"DROP CORTEX SEARCH SERVICE IF EXISTS {PROBE_SCHEMA}.PROBE_SVC2"],
        ),
        Probe(
            id="LEGACY_SENTIMENT",
            group="cortex_functions",
            why="Pre-AI_* namespace; gated separately in principle",
            statements=["SELECT SNOWFLAKE.CORTEX.SENTIMENT('this is terrible')"],
        ),
        Probe(
            id="LEGACY_SUMMARIZE",
            group="cortex_functions",
            why="Pre-AI_* namespace summarisation",
            statements=["SELECT SNOWFLAKE.CORTEX.SUMMARIZE('The quick brown fox jumped.')"],
        ),
        Probe(
            id="LEGACY_TRANSLATE",
            group="cortex_functions",
            why="Pre-AI_* namespace translation",
            statements=["SELECT SNOWFLAKE.CORTEX.TRANSLATE('hello', 'en', 'hi')"],
        ),
        Probe(
            id="LEGACY_EXTRACT_ANSWER",
            group="cortex_functions",
            why="Extractive QA over a clause; a possible AI_EXTRACT substitute",
            statements=[
                "SELECT SNOWFLAKE.CORTEX.EXTRACT_ANSWER("
                "'The threshold is Rs 10,00,000.', 'What is the threshold?')"
            ],
        ),
        Probe(
            id="AI_TRANSLATE",
            group="cortex_functions",
            why="Transliteration handling for Indian-name screening",
            statements=["SELECT AI_TRANSLATE('hello', 'en', 'hi')"],
        ),
        Probe(
            id="AI_ESTIMATE_QUERY_COST",
            group="cortex_functions",
            why="Pre-flight AI spend control (models.yml:limits)",
            statements=["SELECT AI_ESTIMATE_QUERY_COST('SELECT 1')"],
        ),
    ]

    # ------------------------------------------------------------ governance #
    probes += [
        Probe(
            id="ROW_ACCESS_POLICY",
            group="governance",
            why="'Same question, two roles, two answers' -- the RLS demo beat (G1)",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.RAP_T (ID NUMBER, BRANCH_REGION VARCHAR)",
                f"CREATE OR REPLACE ROW ACCESS POLICY {PROBE_SCHEMA}.RAP "
                "AS (region VARCHAR) RETURNS BOOLEAN -> "
                "CURRENT_ROLE() = 'ACCOUNTADMIN' OR region = 'MUMBAI'",
                f"ALTER TABLE {PROBE_SCHEMA}.RAP_T "
                f"ADD ROW ACCESS POLICY {PROBE_SCHEMA}.RAP ON (BRANCH_REGION)",
            ],
            cleanup=[
                f"ALTER TABLE {PROBE_SCHEMA}.RAP_T DROP ROW ACCESS POLICY {PROBE_SCHEMA}.RAP",
                f"DROP ROW ACCESS POLICY IF EXISTS {PROBE_SCHEMA}.RAP",
            ],
        ),
        Probe(
            id="MASKING_POLICY",
            group="governance",
            why="PAN / Aadhaar masking for non-privileged roles (G1)",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.MASK_T (PAN VARCHAR)",
                f"CREATE OR REPLACE MASKING POLICY {PROBE_SCHEMA}.MASK_PAN "
                "AS (val VARCHAR) RETURNS VARCHAR -> "
                "CASE WHEN CURRENT_ROLE() = 'ACCOUNTADMIN' THEN val ELSE '****' END",
                f"ALTER TABLE {PROBE_SCHEMA}.MASK_T MODIFY COLUMN PAN "
                f"SET MASKING POLICY {PROBE_SCHEMA}.MASK_PAN",
            ],
            cleanup=[
                f"ALTER TABLE {PROBE_SCHEMA}.MASK_T MODIFY COLUMN PAN UNSET MASKING POLICY",
                f"DROP MASKING POLICY IF EXISTS {PROBE_SCHEMA}.MASK_PAN",
            ],
        ),
        Probe(
            id="CREATE_TAG",
            group="governance",
            why="Object tagging for the governance inventory",
            statements=[f"CREATE OR REPLACE TAG {PROBE_SCHEMA}.PROBE_TAG"],
        ),
        Probe(
            id="TAG_BASED_MASKING",
            group="governance",
            why="Policy applied once to a tag rather than per column",
            statements=[
                f"ALTER TAG {PROBE_SCHEMA}.PROBE_TAG SET MASKING POLICY {PROBE_SCHEMA}.MASK_PAN"
            ],
            cleanup=[
                f"ALTER TAG {PROBE_SCHEMA}.PROBE_TAG UNSET MASKING POLICY {PROBE_SCHEMA}.MASK_PAN"
            ],
        ),
        Probe(
            id="AGGREGATION_POLICY",
            group="governance",
            why="Prevents single-party disclosure from an aggregate view",
            statements=[
                f"CREATE OR REPLACE AGGREGATION POLICY {PROBE_SCHEMA}.AGG_POL "
                "AS () RETURNS AGGREGATION_CONSTRAINT -> "
                "AGGREGATION_CONSTRAINT(MIN_GROUP_SIZE => 5)"
            ],
            cleanup=[f"DROP AGGREGATION POLICY IF EXISTS {PROBE_SCHEMA}.AGG_POL"],
        ),
        Probe(
            id="PROJECTION_POLICY",
            group="governance",
            why="Blocks a column from being SELECTed while remaining joinable",
            statements=[
                f"CREATE OR REPLACE PROJECTION POLICY {PROBE_SCHEMA}.PROJ_POL "
                "AS () RETURNS PROJECTION_CONSTRAINT -> PROJECTION_CONSTRAINT(ALLOW => FALSE)"
            ],
            cleanup=[f"DROP PROJECTION POLICY IF EXISTS {PROBE_SCHEMA}.PROJ_POL"],
        ),
        Probe(
            id="CREATE_DMF",
            group="governance",
            why="Pipeline-health gates and freshness qualification (G6, Suite 5)",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.DMF_T (ID NUMBER, AMT NUMBER)",
                f"INSERT INTO {PROBE_SCHEMA}.DMF_T VALUES (1, 10), (2, NULL)",
                f"CREATE OR REPLACE DATA METRIC FUNCTION {PROBE_SCHEMA}.DMF_NULL_AMT "
                "(arg_t TABLE(arg_c NUMBER)) RETURNS NUMBER AS "
                "$$ SELECT COUNT(*) FROM arg_t WHERE arg_c IS NULL $$",
            ],
        ),
        Probe(
            id="ASSOCIATE_DMF",
            group="governance",
            why="A DMF only gates a pipeline once associated and scheduled",
            statements=[
                f"ALTER TABLE {PROBE_SCHEMA}.DMF_T SET DATA_METRIC_SCHEDULE = 'TRIGGER_ON_CHANGES'",
                f"ALTER TABLE {PROBE_SCHEMA}.DMF_T ADD DATA METRIC FUNCTION "
                f"{PROBE_SCHEMA}.DMF_NULL_AMT ON (AMT)",
            ],
            cleanup=[
                f"ALTER TABLE {PROBE_SCHEMA}.DMF_T DROP DATA METRIC FUNCTION "
                f"{PROBE_SCHEMA}.DMF_NULL_AMT ON (AMT)"
            ],
        ),
        Probe(
            id="DATA_METRIC_SCAN",
            group="governance",
            why="Ad-hoc quality scan without waiting for the schedule",
            statements=[
                "SELECT * FROM TABLE(SYSTEM$DATA_METRIC_SCAN("
                f"REF_ENTITY_NAME => '{PROBE_SCHEMA}.DMF_T', "
                f"METRIC_NAME => '{PROBE_SCHEMA}.DMF_NULL_AMT', "
                "ARGUMENT_NAME => 'AMT'))"
            ],
        ),
        Probe(
            id="RETENTION_14_DAYS",
            group="governance",
            why="retention.landing_days / evidence_days in conf/profiles/demo.yml",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.RETENTION_T (ID NUMBER)",
                f"ALTER TABLE {PROBE_SCHEMA}.RETENTION_T SET DATA_RETENTION_TIME_IN_DAYS = 14",
            ],
        ),
    ]

    # -------------------------------------------------------------------- ml #
    probes += [
        Probe(
            id="SNOWFLAKE.ML.ANOMALY_DETECTION",
            group="ml",
            why="Velocity anomaly baseline (ml.yml tier1.velocity_anomaly)",
            statements=[
                f"CREATE OR REPLACE VIEW {PROBE_SCHEMA}.ML_SRC AS "
                "SELECT DATEADD('day', SEQ4(), '2026-01-01'::TIMESTAMP_NTZ) AS TS, "
                "UNIFORM(100, 200, RANDOM(42))::FLOAT AS Y "
                "FROM TABLE(GENERATOR(ROWCOUNT => 80))",
                f"CREATE OR REPLACE SNOWFLAKE.ML.ANOMALY_DETECTION {PROBE_SCHEMA}.AD_MODEL("
                f"INPUT_DATA => TABLE({PROBE_SCHEMA}.ML_SRC), "
                "TIMESTAMP_COLNAME => 'TS', TARGET_COLNAME => 'Y', LABEL_COLNAME => '')",
            ],
            cleanup=[f"DROP SNOWFLAKE.ML.ANOMALY_DETECTION IF EXISTS {PROBE_SCHEMA}.AD_MODEL"],
        ),
        Probe(
            id="SNOWFLAKE.ML.FORECAST",
            group="ml",
            why="Not in Tier 1; probed to establish whether the ML class is gated as a whole",
            statements=[
                f"CREATE OR REPLACE SNOWFLAKE.ML.FORECAST {PROBE_SCHEMA}.FC_MODEL("
                f"INPUT_DATA => TABLE({PROBE_SCHEMA}.ML_SRC), "
                "TIMESTAMP_COLNAME => 'TS', TARGET_COLNAME => 'Y')"
            ],
            cleanup=[f"DROP SNOWFLAKE.ML.FORECAST IF EXISTS {PROBE_SCHEMA}.FC_MODEL"],
        ),
        Probe(
            id="SNOWFLAKE.ML.CLASSIFICATION",
            group="ml",
            why="Deferred triage classifier (ml.yml future_scope.triage_classifier)",
            statements=[
                # Labels must be balanced and separable, or training fails for a
                # data reason and the failure reads as a capability gate.
                f"CREATE OR REPLACE VIEW {PROBE_SCHEMA}.CLS_SRC AS "
                "SELECT SEQ4() AS ID, "
                "MOD(SEQ4(), 2)::VARCHAR AS LABEL, "
                "(MOD(SEQ4(), 2) * 50 + UNIFORM(0, 10, RANDOM(1)))::FLOAT AS F1, "
                "(MOD(SEQ4(), 2) * 30 + UNIFORM(0, 10, RANDOM(2)))::FLOAT AS F2 "
                "FROM TABLE(GENERATOR(ROWCOUNT => 400))",
                f"CREATE OR REPLACE SNOWFLAKE.ML.CLASSIFICATION {PROBE_SCHEMA}.CLS_MODEL("
                f"INPUT_DATA => TABLE(SELECT F1, F2, LABEL FROM {PROBE_SCHEMA}.CLS_SRC), "
                "TARGET_COLNAME => 'LABEL')",
            ],
            cleanup=[f"DROP SNOWFLAKE.ML.CLASSIFICATION IF EXISTS {PROBE_SCHEMA}.CLS_MODEL"],
        ),
        Probe(
            id="SNOWFLAKE.ML.TOP_INSIGHTS",
            group="ml",
            why="Explains week-over-week alert-volume shifts (ml.yml future_scope)",
            statements=[
                "SELECT * FROM TABLE(SNOWFLAKE.ML.TOP_INSIGHTS("
                "INPUT_DATA => SYSTEM$REFERENCE('VIEW', "
                f"'{PROBE_SCHEMA}.CLS_SRC'), "
                "LABEL_COLNAME => 'LABEL', METRIC_COLNAME => 'F1'))"
            ],
        ),
        Probe(
            id="ML_MODEL_REGISTRY",
            group="ml",
            why="AUDIT.AGENT_ACTION_LOG.model_version provenance",
            runner=_run_model_registry,
        ),
        Probe(
            id="ML_FEATURE_STORE",
            group="ml",
            why="Point-in-time-correct training sets (ml.yml future_scope.feature_store)",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.FS_SRC (ACCOUNT_ID VARCHAR, AMT NUMBER)",
                f"INSERT INTO {PROBE_SCHEMA}.FS_SRC VALUES ('A', 1), ('B', 2)",
            ],
            runner=_run_feature_store,
        ),
    ]

    # --------------------------------------------------------- critical path #
    probes += [
        Probe(
            id="CREATE_DYNAMIC_TABLE",
            group="critical_path",
            why="All 8 dynamic tables (diagram 4). The whole pipeline.",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.DT_SRC (ID NUMBER, AMT NUMBER)",
                f"INSERT INTO {PROBE_SCHEMA}.DT_SRC VALUES (1, 10), (2, 20)",
                f"CREATE OR REPLACE DYNAMIC TABLE {PROBE_SCHEMA}.DT_PROBE "
                "TARGET_LAG = '1 minute' WAREHOUSE = {warehouse} "
                "REFRESH_MODE = INCREMENTAL INITIALIZE = ON_CREATE "
                f"AS SELECT ID, AMT * 2 AS DOUBLED FROM {PROBE_SCHEMA}.DT_SRC",
            ],
        ),
        Probe(
            id="DYNAMIC_TABLE_REFRESHES",
            group="critical_path",
            why="A dynamic table that does not refresh is a static table",
            statements=[
                f"INSERT INTO {PROBE_SCHEMA}.DT_SRC VALUES (3, 30)",
                f"ALTER DYNAMIC TABLE {PROBE_SCHEMA}.DT_PROBE REFRESH",
                f"SELECT COUNT(*) FROM {PROBE_SCHEMA}.DT_PROBE",
            ],
        ),
        Probe(
            id="STREAM_ON_DYNAMIC_TABLE",
            group="critical_path",
            why="SIGNALS_STREAM -> ROUTE_SIGNALS. The routing design depends on this.",
            statements=[
                f"CREATE OR REPLACE STREAM {PROBE_SCHEMA}.DT_STREAM "
                f"ON DYNAMIC TABLE {PROBE_SCHEMA}.DT_PROBE",
                f"INSERT INTO {PROBE_SCHEMA}.DT_SRC VALUES (4, 40)",
                f"ALTER DYNAMIC TABLE {PROBE_SCHEMA}.DT_PROBE REFRESH",
                f"SELECT COUNT(*) FROM {PROBE_SCHEMA}.DT_STREAM",
            ],
        ),
        Probe(
            id="CREATE_TASK_WAREHOUSE",
            group="critical_path",
            why="ROUTE_SIGNALS scheduling",
            statements=[
                f"CREATE OR REPLACE TASK {PROBE_SCHEMA}.T_WH "
                "WAREHOUSE = {warehouse} SCHEDULE = '1440 MINUTE' "
                f"AS INSERT INTO {PROBE_SCHEMA}.DT_SRC VALUES (99, 99)"
            ],
            # No cleanup: EXECUTE_TASK below needs this task to still exist.
        ),
        Probe(
            id="CREATE_TASK_SERVERLESS",
            group="critical_path",
            why="Serverless avoids holding a warehouse for a 1-minute task",
            statements=[
                f"CREATE OR REPLACE TASK {PROBE_SCHEMA}.T_SRV "
                "USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL' "
                "SCHEDULE = '1440 MINUTE' "
                f"AS INSERT INTO {PROBE_SCHEMA}.DT_SRC VALUES (98, 98)"
            ],
            cleanup=[f"DROP TASK IF EXISTS {PROBE_SCHEMA}.T_SRV"],
        ),
        Probe(
            id="EXECUTE_TASK",
            group="critical_path",
            why="Demo needs to force a run rather than wait for the schedule",
            statements=[f"EXECUTE TASK {PROBE_SCHEMA}.T_WH"],
        ),
        Probe(
            id="PYTHON_STORED_PROCEDURE",
            group="critical_path",
            why="8 agent tool procedures. Needs Anaconda ToS accepted by ORGADMIN.",
            statements=[
                f"""CREATE OR REPLACE PROCEDURE {PROBE_SCHEMA}.PY_PROBE()
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'main'
AS
$$
def main(session):
    return session.sql('SELECT 1').collect()[0][0]
$$""",
                f"CALL {PROBE_SCHEMA}.PY_PROBE()",
            ],
        ),
        Probe(
            id="SQL_STORED_PROCEDURE",
            group="critical_path",
            why="Fallback for the 8 procedures if Python is unavailable",
            statements=[
                f"""CREATE OR REPLACE PROCEDURE {PROBE_SCHEMA}.SQL_PROBE()
RETURNS STRING
LANGUAGE SQL
AS
$$
BEGIN
  RETURN 'ok';
END
$$""",
                f"CALL {PROBE_SCHEMA}.SQL_PROBE()",
            ],
        ),
        Probe(
            id="RECURSIVE_CTE",
            group="critical_path",
            why="Depth-3 mule-ring traversal",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.EDGE (SRC VARCHAR, DST VARCHAR)",
                f"INSERT INTO {PROBE_SCHEMA}.EDGE VALUES ('A','B'),('B','C'),('C','D')",
                "WITH RECURSIVE walk (SRC, DST, DEPTH) AS ("
                f"  SELECT SRC, DST, 1 FROM {PROBE_SCHEMA}.EDGE WHERE SRC = 'A'"
                "  UNION ALL"
                f"  SELECT e.SRC, e.DST, w.DEPTH + 1 FROM {PROBE_SCHEMA}.EDGE e "
                "  JOIN walk w ON e.SRC = w.DST WHERE w.DEPTH < 3"
                ") SELECT COUNT(*) FROM walk",
            ],
        ),
        Probe(
            id="MERGE_WITH_UNIQUE_CONSTRAINT",
            group="critical_path",
            why="Signal idempotency (Gap 11). Prevents duplicate SIGNAL/CASE rows.",
            statements=[
                f"CREATE OR REPLACE TABLE {PROBE_SCHEMA}.SIGNAL_T ("
                "SIGNAL_BUSINESS_KEY VARCHAR NOT NULL, SEVERITY VARCHAR, "
                "CONSTRAINT UK_SIGNAL UNIQUE (SIGNAL_BUSINESS_KEY))",
                f"MERGE INTO {PROBE_SCHEMA}.SIGNAL_T t USING ("
                "SELECT 'RULE|P1|2026-01' AS K, 'HIGH' AS S) s "
                "ON t.SIGNAL_BUSINESS_KEY = s.K "
                "WHEN MATCHED THEN UPDATE SET SEVERITY = s.S "
                "WHEN NOT MATCHED THEN INSERT (SIGNAL_BUSINESS_KEY, SEVERITY) VALUES (s.K, s.S)",
                f"MERGE INTO {PROBE_SCHEMA}.SIGNAL_T t USING ("
                "SELECT 'RULE|P1|2026-01' AS K, 'HIGH' AS S) s "
                "ON t.SIGNAL_BUSINESS_KEY = s.K "
                "WHEN MATCHED THEN UPDATE SET SEVERITY = s.S "
                "WHEN NOT MATCHED THEN INSERT (SIGNAL_BUSINESS_KEY, SEVERITY) VALUES (s.K, s.S)",
                f"SELECT COUNT(*) FROM {PROBE_SCHEMA}.SIGNAL_T",
            ],
        ),
        Probe(
            id="STREAMLIT",
            group="critical_path",
            why="The demo surface",
            statements=[
                f"CREATE OR REPLACE STAGE {PROBE_SCHEMA}.ST_STAGE DIRECTORY = (ENABLE = TRUE)",
                f"CREATE OR REPLACE STREAMLIT {PROBE_SCHEMA}.PROBE_APP "
                f"ROOT_LOCATION = '@{PROBE_SCHEMA}.ST_STAGE' "
                "MAIN_FILE = 'app.py' QUERY_WAREHOUSE = {warehouse}",
            ],
            cleanup=[f"DROP STREAMLIT IF EXISTS {PROBE_SCHEMA}.PROBE_APP"],
        ),
        Probe(
            id="NOTIFICATION_INTEGRATION_EMAIL",
            group="critical_path",
            why="Case routing and notification dispatch (diagram 9)",
            statements=[
                "CREATE OR REPLACE NOTIFICATION INTEGRATION RC_PROBE_EMAIL "
                "TYPE = EMAIL ENABLED = TRUE"
            ],
            # No cleanup: SEND_EMAIL below needs the integration. Dropped in the
            # account-level teardown, since it does not live in the probe database.
        ),
        Probe(
            id="SEND_EMAIL",
            group="critical_path",
            why="An integration that cannot actually send is not a notification path",
            # Must be a verified address belonging to a user of this account.
            # Sending to an arbitrary address fails as a setup condition, not a
            # capability gate, so the probe uses the caller's own address.
            statements=[
                "CALL SYSTEM$SEND_EMAIL('RC_PROBE_EMAIL', "
                "(SELECT email FROM snowflake.account_usage.users "
                "WHERE name = CURRENT_USER() AND deleted_on IS NULL LIMIT 1), "
                "'risk-copilot capability probe', 'Sent by scripts/probe_capabilities.py')"
            ],
        ),
        Probe(
            id="NETWORK_RULE",
            group="critical_path",
            why="Prerequisite for external access (Jira/Slack, diagram 13)",
            statements=[
                f"CREATE OR REPLACE NETWORK RULE {PROBE_SCHEMA}.PROBE_RULE "
                "MODE = EGRESS TYPE = HOST_PORT VALUE_LIST = ('example.com:443')"
            ],
        ),
        Probe(
            id="EXTERNAL_ACCESS_INTEGRATION",
            group="critical_path",
            why="MCP / external loop closing (diagram 13)",
            statements=[
                "CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION RC_PROBE_EAI "
                f"ALLOWED_NETWORK_RULES = ({PROBE_SCHEMA}.PROBE_RULE) ENABLED = TRUE"
            ],
            cleanup=["DROP EXTERNAL ACCESS INTEGRATION IF EXISTS RC_PROBE_EAI"],
        ),
    ]

    return probes


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _run_probe(session: Any, settings: Settings, probe: Probe, warehouse: str) -> Result:
    statements = [s.replace("{warehouse}", warehouse) for s in probe.statements]
    recorded = statements[-1] if statements else "<python runner>"

    try:
        for sql in statements:
            session.sql(sql).collect()
        if probe.runner is not None:
            recorded = probe.runner(session, settings)
    except Exception as exc:
        text = str(exc).strip()
        failure = classify(text)
        if probe.resolution_only and failure not in ("a_absent", "b_edition", "c_trial"):
            return Result(
                id=probe.id,
                group=probe.group,
                why=probe.why,
                available=True,
                statement=" ".join(recorded.split()),
                note=(
                    "Entry point resolves on this account; not exercised end to end. "
                    f"Rejected the probe fixture with: {text.splitlines()[-1][:220]}"
                ),
            )
        return Result(
            id=probe.id,
            group=probe.group,
            why=probe.why,
            available=False,
            statement=" ".join(recorded.split()),
            failure_class=failure,
            error_code=_error_code(text),
            error=text[:900],
        )
    finally:
        for sql in probe.cleanup:
            # Teardown is best effort: a cleanup failure must not mask the probe
            # result, and the database drop is the real guarantee.
            with suppress(Exception):
                session.sql(sql.replace("{warehouse}", warehouse)).collect()

    return Result(
        id=probe.id,
        group=probe.group,
        why=probe.why,
        available=True,
        statement=" ".join(recorded.split()),
    )


def account_facts(session: Any) -> dict[str, Any]:
    """Collect the per-account facts the comparison table needs."""
    facts: dict[str, Any] = {}

    def scalar(sql: str, key: str) -> None:
        try:
            facts[key] = session.sql(sql).collect()[0][0]
        except Exception as exc:
            facts[key] = f"<unavailable: {str(exc).splitlines()[0][:120]}>"

    scalar("SELECT CURRENT_ACCOUNT()", "account_locator")
    scalar("SELECT CURRENT_ORGANIZATION_NAME()", "organization")
    scalar("SELECT CURRENT_REGION()", "region")
    scalar("SELECT CURRENT_USER()", "user")
    scalar("SELECT CURRENT_VERSION()", "snowflake_version")
    scalar("SELECT CURRENT_AVAILABLE_ROLES()", "available_roles")

    # Edition, and whether this is a trial. The ORGANIZATION_USAGE view is
    # readable by ACCOUNTADMIN; SHOW ORGANIZATION ACCOUNTS needs ORGADMIN but
    # carries fields the view does not. Try both and record which answered.
    try:
        rows = session.sql(
            "SELECT edition, account_name, created_on "
            "FROM snowflake.organization_usage.accounts "
            "WHERE account_locator = CURRENT_ACCOUNT() AND deleted_on IS NULL "
            "ORDER BY created_on DESC LIMIT 1"
        ).collect()
        if rows:
            mapping = {k.upper(): v for k, v in rows[0].as_dict().items()}
            facts["edition"] = mapping.get("EDITION")
            facts["account_name"] = mapping.get("ACCOUNT_NAME")
            facts["created_on"] = str(mapping.get("CREATED_ON"))
            facts["edition_source"] = "SNOWFLAKE.ORGANIZATION_USAGE.ACCOUNTS"
    except Exception as exc:
        facts["edition_view_error"] = str(exc).splitlines()[-1][:200]

    try:
        session.sql("USE ROLE ORGADMIN").collect()
        facts["holds_orgadmin"] = True
        rows = session.sql("SHOW ORGANIZATION ACCOUNTS").collect()
        locator = facts.get("account_locator")
        for row in rows:
            mapping = row.as_dict()
            if mapping.get("account_locator") == locator:
                facts.setdefault("edition", mapping.get("edition"))
                facts["show_org_accounts_fields"] = sorted(mapping)
                # The trial marker's name varies by release; record whatever is
                # present rather than inferring trial status from a guess.
                for key in mapping:
                    if "trial" in key.lower() or "balance" in key.lower():
                        facts[f"org_{key}"] = mapping.get(key)
                break
    except Exception as exc:
        facts["holds_orgadmin"] = False
        facts["show_org_accounts_error"] = str(exc).splitlines()[-1][:200]
    finally:
        session.sql(f"USE ROLE {PROBE_ROLE}").collect()

    for param in (
        "CORTEX_ENABLED_CROSS_REGION",
        "CORTEX_MODELS_ALLOWLIST",
        "DATA_RETENTION_TIME_IN_DAYS",
    ):
        # A parameter the role cannot read is simply absent from the facts, which
        # is more honest than recording a guessed default.
        with suppress(Exception):
            rows = session.sql(f"SHOW PARAMETERS LIKE '{param}' IN ACCOUNT").collect()
            if rows:
                facts[f"param_{param.lower()}"] = rows[0].as_dict().get("value")

    # Trial credit balance. Its presence and a non-zero FREE_USAGE_BALANCE is the
    # only reliable trial indicator found: SHOW ORGANIZATION ACCOUNTS returns no
    # rows on this account even under ORGADMIN, so there is no is_trial flag to read.
    try:
        rows = session.sql(
            "SELECT currency, free_usage_balance, capacity_balance, "
            "on_demand_consumption_balance, date "
            "FROM snowflake.organization_usage.remaining_balance_daily "
            "ORDER BY date DESC LIMIT 1"
        ).collect()
        if rows:
            facts["remaining_balance"] = {
                k.lower(): (float(v) if isinstance(v, Decimal) else str(v))
                for k, v in rows[0].as_dict().items()
            }
    except Exception as exc:
        facts["remaining_balance"] = f"<unavailable: {str(exc).splitlines()[-1][:160]}>"

    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="Leave the probe database in place for manual inspection.",
    )
    args = parser.parse_args()

    # RC_SNOWFLAKE_PRIVATE_KEY_PATH lives in .env, which the CLI loads but the
    # library does not. The Analyst REST probe needs the key to mint a JWT, so
    # without this it would report a capability failure that is really local.
    from risk_copilot.cli import _load_dotenv

    _load_dotenv()

    settings = Settings.load(profile=args.profile)
    session = create_session(settings)
    warehouse = settings.obj(settings.warehouse("dev").name)

    results: list[Result] = []
    try:
        session.sql(f"USE ROLE {PROBE_ROLE}").collect()
        session.sql(f"USE WAREHOUSE {warehouse}").collect()
        facts = account_facts(session)
        facts["probe_role"] = PROBE_ROLE
        print(
            f"account: {facts.get('account_locator')} edition={facts.get('edition')} "
            f"region={facts.get('region')}"
        )

        session.sql(f"CREATE DATABASE IF NOT EXISTS {PROBE_DB}").collect()
        session.sql(f"CREATE SCHEMA IF NOT EXISTS {PROBE_SCHEMA}").collect()
        session.sql(f"USE SCHEMA {PROBE_SCHEMA}").collect()
        stage_fixture(session)

        for probe in build_probes():
            result = _run_probe(session, settings, probe, warehouse)
            results.append(result)
            mark = "OK  " if result.available else f"FAIL[{result.failure_class}]"
            print(f"{mark} {result.id}")
            if not result.available and result.error:
                print(f"       {result.error.splitlines()[-1][:170]}")
    finally:
        dropped = False
        if not args.keep_db:
            try:
                session.sql(f"DROP DATABASE IF EXISTS {PROBE_DB} CASCADE").collect()
                remaining = session.sql(f"SHOW DATABASES LIKE '{PROBE_DB}'").collect()
                dropped = not remaining
                print(f"probe database dropped: {dropped}")
            except Exception as exc:
                print(f"WARNING: could not drop {PROBE_DB}: {exc}")
        # Account-level objects live outside the probe database, so dropping the
        # database does not remove them. Unconditional, so a probe that failed
        # midway still leaves nothing behind.
        for sql in (
            "DROP NOTIFICATION INTEGRATION IF EXISTS RC_PROBE_EMAIL",
            "DROP EXTERNAL ACCESS INTEGRATION IF EXISTS RC_PROBE_EAI",
        ):
            with suppress(Exception):
                session.sql(sql).collect()

    out = _write(settings, facts, results, probe_db_dropped=dropped)
    session.close()
    print(f"\nwrote {out}")
    failed = [r for r in results if not r.available]
    print(f"{len(results)} probes: {len(results) - len(failed)} available, {len(failed)} not")
    return 0


def _write(
    settings: Settings,
    facts: dict[str, Any],
    results: list[Result],
    *,
    probe_db_dropped: bool,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "probed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "connection": settings.connection_name,
        "profile": settings.profile,
        "probe_database_dropped": probe_db_dropped,
        "account": facts,
        "failure_classes": {
            "a_absent": "object or function does not exist in this account/region",
            "b_edition": "requires Enterprise Edition or higher",
            "c_trial": "blocked because the account is a trial",
            "d_privilege": "missing grant; fixable without changing accounts",
            "e_precondition": "unmet precondition (Anaconda terms, data, warehouse)",
            "f_unclassified": "recorded verbatim; not classified",
        },
        "probes": {},
    }
    for result in results:
        entry: dict[str, Any] = {
            "group": result.group,
            "available": result.available,
            "why_it_matters": result.why,
            "statement": result.statement,
        }
        if not result.available:
            entry["failure_class"] = result.failure_class
            entry["error_code"] = result.error_code
            entry["error"] = result.error
        if result.note:
            entry["note"] = result.note
        payload["probes"][result.id] = entry

    out = settings.repo_root / "conf" / "generated" / f"capabilities.{settings.connection_name}.yml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(payload, sort_keys=False, width=100), encoding="utf-8")
    return out


if __name__ == "__main__":
    sys.exit(main())
