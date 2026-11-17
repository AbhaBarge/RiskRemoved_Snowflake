"""The investigator agent: its deploy stage, config, clients and Slack routing.

No Snowflake connection. Agent responses are synthetic but shaped like the
live agent:run output (streamed SSE and the non-streaming JSON body), with
made-up ids and figures.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from risk_copilot.config import load_settings
from risk_copilot.pipeline.app import app_config
from risk_copilot.pipeline.deploy import STAGE_ORDER, Deployer, split_statements


@pytest.fixture
def deployer(repo_root: Path) -> Deployer:
    return Deployer(load_settings(repo_root=repo_root, profile="dev"))


def _stage(deployer: Deployer) -> dict[str, str]:
    return {p.name: deployer.render(p) for p in deployer.stage_files("investigator")}


def _code(statement: str) -> str:
    lines = [line for line in statement.splitlines() if not line.strip().startswith("--")]
    return " ".join(" ".join(lines).split()).upper()


# --------------------------------------------------------------------------- #
# Deploy stage
# --------------------------------------------------------------------------- #


def test_the_investigator_is_its_own_stage_after_app_access() -> None:
    keys = [key for key, _ in STAGE_ORDER]
    assert keys.index("app_access") < keys.index("investigator")
    # Deploying it must not re-run 08_agent's CREATE OR REPLACE AGENT.
    assert dict(STAGE_ORDER)["investigator"] != dict(STAGE_ORDER)["agent"]


def test_names_carry_the_profile_suffix(deployer: Deployer) -> None:
    ctx = deployer.settings.sql_context()["cortex"]
    assert ctx["investigator_agent"] == "RC_INVESTIGATOR_AGENT_DEV"
    assert ctx["investigator_context_proc"] == "GET_CASE_CONTEXT_DEV"


def test_the_stage_renders_without_lint_errors(deployer: Deployer) -> None:
    files = _stage(deployer)
    assert set(files) == {"01_case_context.sql", "02_agent.sql", "03_grants.sql"}
    errors = [e for e in deployer.lint() if "12_investigator" in e]
    assert not errors, errors


def test_the_stage_is_idempotent_and_never_destructive(deployer: Deployer) -> None:
    for name, sql in _stage(deployer).items():
        for statement in split_statements(sql):
            head = _code(statement)
            assert not head.startswith(("DROP ", "TRUNCATE ", "DELETE ")), f"{name}: {head[:100]}"
            if head.startswith("CREATE"):
                assert "OR REPLACE" in head or "IF NOT EXISTS" in head, f"{name}: {head[:100]}"


def test_the_case_context_tool_only_reads(deployer: Deployer) -> None:
    sql = _stage(deployer)["01_case_context.sql"]
    body = sql.split("$$")[1].upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "TRUNCATE ", "CREATE ", "DROP "):
        assert verb not in body, f"GET_CASE_CONTEXT must be read-only; found {verb.strip()}"
    assert "EXECUTE AS OWNER" in sql.upper()


def test_the_case_context_tool_returns_no_identity_fields(deployer: Deployer) -> None:
    body = _stage(deployer)["01_case_context.sql"].split("$$")[1].lower()
    for column in ("full_name", "pan_number", "aadhaar", "phone_number", "tax_id"):
        assert column not in body, column


def _spec(deployer: Deployer) -> dict:
    sql = _stage(deployer)["02_agent.sql"]
    return yaml.safe_load(sql.split("$$")[1])


def test_the_agent_spec_wires_both_tools_to_this_profile(deployer: Deployer) -> None:
    spec = _spec(deployer)
    tools = {t["tool_spec"]["name"]: t["tool_spec"]["type"] for t in spec["tools"]}
    assert tools == {"case_context": "generic", "RiskDataAnalyst": "cortex_analyst_text_to_sql"}
    # A tool without a resource of the same name is silently unusable.
    assert set(spec["tool_resources"]) == set(tools)
    res = spec["tool_resources"]
    assert (
        res["case_context"]["identifier"] == "RISK_COPILOT_DEV.EVIDENCE_CHAIN.GET_CASE_CONTEXT_DEV"
    )
    assert res["RiskDataAnalyst"]["semantic_view"] == "RISK_COPILOT_DEV.CURATED.SV_RISK_COPILOT_DEV"
    for resource in res.values():
        assert resource["execution_environment"]["warehouse"] == "RC_DEMO_WH_DEV"


def test_the_instructions_require_standalone_analyst_questions(deployer: Deployer) -> None:
    orchestration = _spec(deployer)["instructions"]["orchestration"]
    assert "standalone" in orchestration
    assert "case_context" in orchestration and "RiskDataAnalyst" in orchestration
    # Bank-wide questions must not inherit the case under discussion.
    assert "NOT scoped to the case" in orchestration


def test_every_caller_role_is_granted_the_agent_tool_and_view(deployer: Deployer) -> None:
    grants = [_code(s) for s in split_statements(_stage(deployer)["03_grants.sql"])]
    agent = "RISK_COPILOT_DEV.EVIDENCE_CHAIN.RC_INVESTIGATOR_AGENT_DEV"
    proc = "RISK_COPILOT_DEV.EVIDENCE_CHAIN.GET_CASE_CONTEXT_DEV(VARCHAR)"
    view = "RISK_COPILOT_DEV.CURATED.SV_RISK_COPILOT_DEV"
    for role in ("RC_SERVICE_ROLE_DEV", "COMPLIANCE_OFFICER_DEV", "RC_PUBLIC_APP_DEV"):
        assert f"GRANT USAGE ON AGENT {agent} TO ROLE {role}" in grants
        assert f"GRANT USAGE ON PROCEDURE {proc} TO ROLE {role}" in grants
        assert f"GRANT SELECT ON SEMANTIC VIEW {view} TO ROLE {role}" in grants
    assert all(g.startswith("GRANT ") for g in grants)


def test_the_app_is_told_which_agent_answers_ask(repo_root: Path) -> None:
    cfg = app_config(load_settings(repo_root=repo_root, profile="dev"))
    assert cfg["agent"] == {
        "investigator": "RISK_COPILOT_DEV.EVIDENCE_CHAIN.RC_INVESTIGATOR_AGENT_DEV",
        "ask_backend": "agent",
    }


def test_an_unknown_ask_backend_is_rejected(repo_root: Path) -> None:
    from risk_copilot.config import AppSpec, ConfigError

    base = load_settings(repo_root=repo_root, profile="dev").app.model_dump(by_alias=True)
    with pytest.raises((ConfigError, ValueError)):
        AppSpec(**{**base, "ask_backend": "magic"})


# --------------------------------------------------------------------------- #
# Bridge client: agent:run parsing (src/risk_copilot/agentic/agent_client.py)
# --------------------------------------------------------------------------- #

_RESULT_SET = {
    "data": [["MUMBAI", "1500.50", "3"], ["DELHI", "900.00", "2"]],
    "resultSetMetaData": {
        "format": "jsonv2",
        "numRows": 2,
        "rowType": [
            {"name": "BRANCH", "type": "text", "scale": 0},
            {"name": "AMOUNT_INR", "type": "fixed", "scale": 2},
            {"name": "DEPOSITS", "type": "fixed", "scale": 0},
        ],
    },
}
_SQL = "SELECT branch, SUM(amount_inr) AS amount_inr, COUNT(*) AS deposits FROM t GROUP BY 1"


def _sse(*events: tuple[str, dict]) -> list[str]:
    lines: list[str] = []
    for kind, data in events:
        lines += [f"event: {kind}", f"data: {json.dumps(data)}", ""]
    return lines


def test_a_streamed_turn_is_assembled_with_its_analyst_query() -> None:
    from risk_copilot.agentic import agent_client

    stream = _sse(
        (
            "response.tool_use",
            {"tool_use_id": "t1", "name": "RiskDataAnalyst", "input": {"q": "x"}},
        ),
        (
            "response.tool_result",
            {
                "tool_use_id": "t1",
                "status": "success",
                "content": [{"type": "json", "json": {"semantic_model_name": "sv", "tables": []}}],
            },
        ),
        (
            "response.tool_use",
            {"tool_use_id": "t2", "name": "system_execute_sql", "input": {"sql": "..."}},
        ),
        (
            "response.tool_result",
            {
                "tool_use_id": "t2",
                "status": "success",
                "content": [
                    {
                        "type": "json",
                        "json": {"sql": _SQL, "result_set": _RESULT_SET, "query_id": "q-1"},
                    }
                ],
            },
        ),
        ("response.text.delta", {"text": "MUMBAI leads "}),
        ("response.text.delta", {"text": "\u2013 Rs 1,500.50."}),
        ("metadata", {"metadata": {"role": "assistant", "message_id": 42}}),
    )
    out = agent_client.AgentResult()
    agent_client._consume(
        SimpleNamespace(iter_lines=lambda decode_unicode=True: iter(stream)), out, None
    )

    assert out.text == "MUMBAI leads \u2013 Rs 1,500.50."
    assert out.assistant_message_id == 42
    assert [c["name"] for c in out.tool_calls] == ["RiskDataAnalyst", "system_execute_sql"]
    # Only the SQL actually run counts; the semantic-model result carries none.
    assert out.analyst_sql == [_SQL]
    q = out.analyst_queries[0]
    assert [c["name"] for c in q["columns"]] == ["BRANCH", "AMOUNT_INR", "DEPOSITS"]
    assert q["rows"] == [["MUMBAI", 1500.5, 3], ["DELHI", 900.0, 2]]


def test_the_investigator_states_the_threads_case_as_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from risk_copilot.agentic import agent_client

    sent: dict = {}
    monkeypatch.setattr(agent_client, "investigator_endpoint", lambda: "EP")
    monkeypatch.setattr(
        agent_client, "run_agent", lambda message, **kw: sent.update(message=message, **kw)
    )
    agent_client.run_investigator(
        "and the total?", thread_id=7, parent_message_id=3, case_id="CASE-abc"
    )
    assert sent["message"].endswith("and the total?")
    assert "case CASE-abc" in sent["message"]
    assert (sent["endpoint"], sent["thread_id"], sent["parent_message_id"]) == ("EP", 7, 3)


def test_calls_without_an_endpoint_are_mains_str_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from risk_copilot.agentic import agent_client

    for var in ("AGENT_DATABASE", "AGENT_SCHEMA", "AGENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_SNOWFLAKE_ACCOUNT", "AGT-1")
    monkeypatch.setattr(agent_client, "_jwt", lambda: "TOKEN")
    assert agent_client._url_for(None) == "https://agt-1.snowflakecomputing.com"
    assert agent_client._headers_for(None) == agent_client._headers()
    assert "X-Snowflake-Role" not in agent_client._headers_for(None, stream=True)
    assert agent_client.agent_target().agent_fq == "RISK_COPILOT_AGENT.STR.RC_STR_AGENT"


def test_the_investigator_endpoint_follows_the_data_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from risk_copilot.agentic import connections

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ACCT-1")
    monkeypatch.setenv("SNOWFLAKE_USER", "SVC")
    monkeypatch.setenv("SNOWFLAKE_ROLE", "RC_SERVICE_ROLE_DEV")
    monkeypatch.setenv("DATA_DATABASE", "RISK_COPILOT_DEV")
    monkeypatch.delenv("DATA_OBJECT_SUFFIX", raising=False)
    monkeypatch.delenv("INVESTIGATOR_AGENT_NAME", raising=False)
    ep = connections.investigator_endpoint()
    assert (ep.account, ep.user, ep.role) == ("ACCT-1", "SVC", "RC_SERVICE_ROLE_DEV")
    assert ep.target.agent_fq == "RISK_COPILOT_DEV.EVIDENCE_CHAIN.RC_INVESTIGATOR_AGENT_DEV"


def test_a_stale_passphrase_does_not_break_an_unencrypted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from risk_copilot.agentic import connections

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "plain.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("K", str(path))
    monkeypatch.setenv("P", "left-over-passphrase")
    loaded = connections.load_private_key("K", "P")
    assert loaded.public_key().public_numbers() == key.public_key().public_numbers()


# --------------------------------------------------------------------------- #
# Slack routing (bridge/ask_routing.py)
# --------------------------------------------------------------------------- #


@pytest.fixture
def routing(repo_root: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pytest.importorskip("tabulate")
    monkeypatch.syspath_prepend(str(repo_root / "bridge"))
    import ask_routing

    return ask_routing


@pytest.mark.parametrize(
    ("kind", "str_thread", "about_draft", "backend", "ok", "expected"),
    [
        ("pdf", False, False, "agent", True, "pdf"),
        ("str", True, False, "agent", True, "str"),
        ("agent", False, True, "agent", True, "agent"),
        # Inside an STR thread, the draft itself is the STR agent's.
        ("analyst", True, True, "agent", True, "agent"),
        # ...but a data question there goes to the investigator.
        ("analyst", True, False, "agent", True, "investigator"),
        ("analyst", False, False, "agent", True, "investigator"),
        # Rollback switch, and an unavailable investigator, both mean direct Analyst.
        ("analyst", False, False, "analyst", True, "analyst"),
        ("analyst", False, False, "agent", False, "analyst"),
        # Without the investigator, main's rule: every STR-thread reply is the STR agent's.
        ("analyst", True, False, "analyst", True, "agent"),
        ("analyst", True, False, "agent", False, "agent"),
    ],
)
def test_routing(routing, kind, str_thread, about_draft, backend, ok, expected) -> None:  # type: ignore[no-untyped-def]
    got = routing.decide(
        kind, str_thread=str_thread, about_draft=about_draft, backend=backend, investigator_ok=ok
    )
    assert got == expected


def test_markdown_becomes_slack_mrkdwn(routing) -> None:  # type: ignore[no-untyped-def]
    assert routing.to_slack_mrkdwn("**Total:** Rs 5\n## Grounds") == "*Total:* Rs 5\n*Grounds*"
    assert routing.to_slack_mrkdwn("[docs](https://example.com)") == "<https://example.com|docs>"


def test_investigator_blocks_show_answer_table_sql_and_tools(routing) -> None:  # type: ignore[no-untyped-def]
    from risk_copilot.agentic.agent_client import AgentResult

    r = AgentResult(text="**MUMBAI** leads.", latency_ms=1234)
    r.tool_calls = [
        {"name": "case_context"},
        {"name": "system_execute_sql"},
        {"name": "system_execute_sql"},
    ]
    r.tool_results = [
        {"name": "system_execute_sql", "result": {"sql": _SQL, "result_set": _RESULT_SET}}
    ]
    blocks = routing.investigator_blocks(r, agent_name="RC_INVESTIGATOR_AGENT_DEV")
    texts = [b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"]
    assert texts[0] == "*MUMBAI* leads."
    assert "MUMBAI" in texts[1] and "1500.5" in texts[1]
    assert _SQL in texts[2]
    footer = blocks[-1]["elements"][0]["text"]
    assert "case_context, system_execute_sql" in footer and "1234ms" in footer
    assert all(len(t) <= 3000 for t in texts)


def test_an_agent_error_is_shown_not_swallowed(routing) -> None:  # type: ignore[no-untyped-def]
    from risk_copilot.agentic.agent_client import AgentResult

    blocks = routing.investigator_blocks(
        AgentResult(error="agent:run returned 403"), agent_name="A"
    )
    assert "403" in blocks[1]["text"]["text"]


# --------------------------------------------------------------------------- #
# App client (app/lib/investigator.py): the non-streaming response
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_investigator(repo_root: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.syspath_prepend(str(repo_root / "app"))
    from lib import investigator

    return investigator


def test_the_app_parses_a_non_streaming_answer(app_investigator) -> None:  # type: ignore[no-untyped-def]
    body = {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": {"text": "..."}},
            {"type": "tool_use", "tool_use": {"name": "RiskDataAnalyst", "input": {}}},
            {"type": "tool_use", "tool_use": {"name": "system_execute_sql", "input": {}}},
            {
                "type": "tool_result",
                "tool_result": {
                    "name": "system_execute_sql",
                    "content": [{"type": "json", "json": {"sql": _SQL, "result_set": _RESULT_SET}}],
                },
            },
            {"type": "text", "text": "MUMBAI leads."},
            {"type": "table", "table": {"title": "By branch", "result_set": _RESULT_SET}},
            {"type": "suggested_queries", "suggested_queries": [{"query": "And by day?"}]},
        ],
        # The id the next turn must pass as parent_message_id lives here.
        "metadata": {"assistant_message_id": 99, "thread_id": 5, "orchestration_model": "m"},
    }
    out = app_investigator.parse(body)
    assert out["text"] == "MUMBAI leads."
    assert out["tools"] == ["RiskDataAnalyst", "system_execute_sql"]
    assert out["sql"] == _SQL and out["sqls"] == [_SQL]
    assert out["suggestions"] == ["And by day?"]
    assert (out["assistant_message_id"], out["thread_id"], out["model"]) == (99, 5, "m")
    assert out["tables"][0]["title"] == "By branch"


def test_separate_text_parts_do_not_run_together(app_investigator) -> None:  # type: ignore[no-untyped-def]
    body = {
        "content": [
            {"type": "text", "text": "Cases by status, MUMBAI."},
            {"type": "text", "text": "3 of the 10 cases are in MUMBAI."},
        ]
    }
    assert (
        app_investigator.parse(body)["text"]
        == "Cases by status, MUMBAI.\n\n3 of the 10 cases are in MUMBAI."
    )


def test_the_app_types_result_set_columns(app_investigator) -> None:  # type: ignore[no-untyped-def]
    df = app_investigator.frame(_RESULT_SET)
    assert list(df.columns) == ["branch", "amount_inr", "deposits"]
    assert df["amount_inr"].tolist() == [1500.5, 900.0]
    assert df["deposits"].tolist() == [3, 2]
    assert app_investigator.frame(None) is None


def test_the_app_only_uses_the_agent_when_configured(app_investigator, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        app_investigator, "CFG", {"agent": {"ask_backend": "analyst", "investigator": "A.B.C"}}
    )
    assert app_investigator.agent_name() is None
    monkeypatch.setattr(
        app_investigator, "CFG", {"agent": {"ask_backend": "agent", "investigator": "A.B.C"}}
    )
    assert app_investigator.agent_name() == "A.B.C"
    monkeypatch.setattr(app_investigator, "CFG", {})
    assert app_investigator.agent_name() is None


def test_ask_page_falls_back_to_analyst(repo_root: Path) -> None:
    """The page must keep a direct-Analyst path for rollback and agent failures."""
    page = (repo_root / "app" / "app_pages" / "ask.py").read_text("utf-8")
    assert re.search(r"turn = _ask_agent\(question\) if AGENT else None", page)
    assert "_ask_analyst(" in page and "ask_fallback_reason" in page
