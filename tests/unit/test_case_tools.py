"""The case tools (sql/procedures): offline rendering and the invariants REVIEW_STR relies on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from risk_copilot.config import load_settings
from risk_copilot.pipeline.deploy import Deployer, split_statements


def _rendered(repo_root: Path, profile: str) -> dict[str, str]:
    d = Deployer(load_settings(repo_root=repo_root, profile=profile))
    return {f.name: d.render(f) for f in d.stage_files("procedures")}


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_procedures_stage_renders_one_object_per_statement(repo_root: Path, profile: str) -> None:
    files = _rendered(repo_root, profile)
    assert sorted(files) == [
        "01_case_tools_common.sql",
        "02_gather_evidence.sql",
        "03_draft_str.sql",
    ]
    assert [len(split_statements(s)) for s in files.values()] == [3, 1, 1]


def test_tool_names_carry_the_profile_suffix(repo_root: Path) -> None:
    dev = _rendered(repo_root, "dev")
    assert (
        "PROCEDURE RISK_COPILOT_DEV.EVIDENCE_CHAIN.GATHER_EVIDENCE_DEV("
        in dev["02_gather_evidence.sql"]
    )
    assert "PROCEDURE RISK_COPILOT_DEV.EVIDENCE_CHAIN.DRAFT_STR_DEV(" in dev["03_draft_str.sql"]
    # DRAFT_STR calls the same profile's gatherer, not another profile's.
    assert "CALL RISK_COPILOT_DEV.EVIDENCE_CHAIN.GATHER_EVIDENCE_DEV(" in dev["03_draft_str.sql"]
    demo = _rendered(repo_root, "demo")
    assert (
        "PROCEDURE RISK_COPILOT.EVIDENCE_CHAIN.GATHER_EVIDENCE(" in demo["02_gather_evidence.sql"]
    )


def _logged_queries(sql: str) -> dict[str, str]:
    """The query texts GATHER_EVIDENCE stores, unescaped from their DEFAULT literals."""
    found = re.findall(r"(q_\w+) VARCHAR DEFAULT '(.*?)';\n", sql, flags=re.S)
    return {name: text.replace("''", "'") for name, text in found}


def test_every_logged_query_takes_exactly_one_bind(repo_root: Path) -> None:
    # REVIEW_STR re-runs a log as EXECUTE IMMEDIATE <sql_text> USING (<params[0]>).
    queries = _logged_queries(_rendered(repo_root, "dev")["02_gather_evidence.sql"])
    assert sorted(queries) == ["q_clauses", "q_deposits", "q_party", "q_summary"]
    for name, text in queries.items():
        assert text.count("?") == 1, name
        assert ";" not in text, name


def test_logged_queries_are_fully_qualified(repo_root: Path) -> None:
    # A log is re-run long after deploy, from any schema: no relative names.
    queries = _logged_queries(_rendered(repo_root, "dev")["02_gather_evidence.sql"])
    for name, text in queries.items():
        for table in re.findall(r"\b(?:FROM|JOIN)\s+([A-Z_][\w.]*)", text):
            if table in {"p", "ids", "d", "r", "span"}:
                continue
            assert table.startswith("RISK_COPILOT_DEV."), (name, table)


def test_the_clause_list_includes_the_filing_duties(repo_root: Path) -> None:
    q = _logged_queries(_rendered(repo_root, "dev")["02_gather_evidence.sql"])["q_clauses"]
    assert "'STR_FILING_SLA_DAYS'" in q and "'PRINCIPAL_OFFICER_ATTESTATION_REQUIRED'" in q


def test_the_narrative_template_holds_no_figure(repo_root: Path) -> None:
    common = _rendered(repo_root, "dev")["01_case_tools_common.sql"]
    (template,) = re.findall(r"'narrative_template', '(.*?)'\n", common)
    assert re.findall(r"\{([a-z_]+)\}", template)
    assert not re.search(r"\d", template), "a number in the template would bypass computed_values"


def test_draft_records_the_configured_narrative_author(repo_root: Path) -> None:
    draft = _rendered(repo_root, "dev")["03_draft_str.sql"]
    assert "'TEMPLATE_V1'" in draft
    assert "FIU_IND_STR" in draft
