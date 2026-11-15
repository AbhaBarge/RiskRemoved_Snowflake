"""Tests for the SQL deployer.

The lint checks here exist because both failure modes they catch are silent at
render time and expensive at execution time:

* a literal object name means the profile suffix stops applying, so a dev deploy
  can overwrite demo objects;
* adjacent string literals are a Snowflake syntax error, surfacing only after
  earlier statements in the file have already applied.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from risk_copilot.config import ConfigError, load_settings
from risk_copilot.pipeline.deploy import STAGE_ORDER, Deployer, split_statements

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# split_statements
# --------------------------------------------------------------------------- #


def test_splits_on_semicolons() -> None:
    assert len(split_statements("SELECT 1; SELECT 2; SELECT 3;")) == 3


def test_ignores_trailing_comment_after_the_last_statement() -> None:
    # Otherwise the comment is sent to Snowflake as a statement and errors.
    assert len(split_statements("SELECT 1;\n-- done\n")) == 1


def test_semicolon_inside_a_dollar_quoted_body_does_not_split() -> None:
    # Python stored-procedure bodies are full of semicolons; splitting inside one
    # would truncate the procedure.
    sql = """
CREATE PROCEDURE p() RETURNS STRING LANGUAGE PYTHON AS $$
def main(session):
    x = 1; y = 2
    return str(x + y)
$$;
SELECT 1;
"""
    statements = split_statements(sql)
    assert len(statements) == 2
    assert "def main" in statements[0]


def test_semicolon_inside_a_string_literal_does_not_split() -> None:
    # Regression: a semicolon inside a COMMENT literal used to split the
    # statement in two, and both halves failed at execution -- after earlier
    # statements in the file had already applied.
    sql = (
        "CREATE SCHEMA s\n"
        "    COMMENT = 'Immutable landing. Append-only; never updated in place.';\n"
        "SELECT 1;\n"
    )
    statements = split_statements(sql)
    assert len(statements) == 2
    assert statements[0].endswith("never updated in place.'")


def test_escaped_quote_inside_a_string_literal_does_not_end_it() -> None:
    sql = "CREATE SCHEMA s COMMENT = 'the Reviewer''s window; kept';\nSELECT 1;\n"
    statements = split_statements(sql)
    assert len(statements) == 2
    assert "Reviewer''s window; kept" in statements[0]


def test_semicolon_inside_a_line_comment_does_not_split() -> None:
    sql = "-- note: a; b; c\nSELECT 1;\n"
    assert len(split_statements(sql)) == 1


def test_semicolon_inside_a_block_comment_does_not_split() -> None:
    sql = "/* a; b; c */\nSELECT 1;\n"
    assert len(split_statements(sql)) == 1


def test_empty_input_yields_no_statements() -> None:
    assert split_statements("\n\n   \n") == []


# --------------------------------------------------------------------------- #
# Rendering the real templates
# --------------------------------------------------------------------------- #


@pytest.fixture
def deployer(repo_root: Path) -> Deployer:
    return Deployer(load_settings(repo_root=repo_root, profile="dev"))


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_every_template_renders_for_every_profile(repo_root: Path, profile: str) -> None:
    errors = Deployer(load_settings(repo_root=repo_root, profile=profile)).lint()
    assert not errors, "Template errors:\n" + "\n".join(errors)


def test_rendered_sql_carries_the_profile_suffix(deployer: Deployer) -> None:
    rendered = deployer.render(deployer.stage_files("ddl")[0])
    assert "RISK_COPILOT_DEV" in rendered
    # The bare name must not appear as a standalone identifier.
    assert "RISK_COPILOT." not in rendered


def test_stages_are_declared_in_dependency_order() -> None:
    keys = [key for key, _ in STAGE_ORDER]
    assert keys.index("bootstrap") < keys.index("ddl") < keys.index("dynamic_tables")
    assert keys.index("dynamic_tables") < keys.index("streams_tasks")
    assert keys.index("ddl") < keys.index("governance")
    # Regression: the agent once deployed before the procedures it calls.
    assert keys.index("procedures") < keys.index("agent")
    assert keys.index("semantic") < keys.index("agent")


def test_agent_has_no_attestation_tool(deployer: Deployer) -> None:
    """Sign-off is a privilege of the officer role, never an agent tool."""
    rendered = "\n".join(deployer.render(p) for p in deployer.stage_files("agent"))
    ctx = deployer.settings.sql_context()
    assert ctx["tool"]["attest_str"] not in rendered
    assert ctx["tool"]["gather_evidence"] in rendered


def test_unknown_stage_is_rejected(deployer: Deployer) -> None:
    with pytest.raises(ConfigError, match="Unknown stage"):
        deployer.stage_files("nonexistent")


def _code(statement: str) -> str:
    """A statement without its leading comment lines, whitespace-normalised, upper-case."""
    lines = [line for line in statement.splitlines() if not line.strip().startswith("--")]
    return " ".join(" ".join(lines).split()).upper()


def test_all_ddl_statements_are_idempotent(deployer: Deployer) -> None:
    # Re-running a stage is the normal way to apply a change, so it must never be
    # a destructive act. Leading comments are stripped first: every statement in
    # these files opens with its header comment, and a check on the raw text
    # would never see the CREATE behind it.
    for stage in ("bootstrap", "ddl", "dynamic_tables", "streams_tasks", "policy"):
        for path in deployer.stage_files(stage):
            for statement in split_statements(deployer.render(path)):
                head = _code(statement)
                if head.startswith(("DROP ", "TRUNCATE ", "DELETE ")):
                    raise AssertionError(f"{path.name}: destructive statement: {head[:110]}")
                if not head.startswith("CREATE"):
                    continue
                assert "IF NOT EXISTS" in head or "OR REPLACE" in head or "OR ALTER" in head, (
                    f"{path.name}: non-idempotent CREATE: {head[:110]}"
                )


def test_ground_truth_stage_is_granted_to_nobody(deployer: Deployer) -> None:
    """Obligation 4: a label must not be readable from a staged file.

    The stage is created, and its ownership handed to the admin role; no other
    GRANT may name it, nor the GROUND_TRUTH schema's objects, in any stage.
    """
    ctx = deployer.settings.sql_context()
    stage = f"{ctx['fq']['ground_truth']}.{ctx['stage']['ground_truth']}"
    created = owned = 0
    for key, _ in STAGE_ORDER:
        for path in deployer.stage_files(key):
            for statement in split_statements(deployer.render(path)):
                head = _code(statement)
                if head.startswith("CREATE STAGE") and stage in head:
                    created += 1
                elif head.startswith("GRANT") and ctx["fq"]["ground_truth"] in head:
                    # Only ownership handed to the admin role may name it.
                    assert head.startswith("GRANT OWNERSHIP "), f"{path.name}: {head[:120]}"
                    assert head.endswith(f"TO ROLE {ctx['role']['admin']} COPY CURRENT GRANTS")
                    owned += stage in head
    assert (created, owned) == (1, 1)


def test_every_stage_and_file_format_is_handed_to_the_admin_role(deployer: Deployer) -> None:
    """M27: objects created as ACCOUNTADMIN must not stay owned by it."""
    ctx = deployer.settings.sql_context()
    rendered = " ".join(
        " ".join(deployer.render(p).split()).upper() for p in deployer.stage_files("bootstrap")
    )
    for name in ctx["stage"].values():
        assert re.search(rf"GRANT OWNERSHIP ON STAGE \S+\.{name} TO ROLE ", rendered), name
    for created in re.findall(r"CREATE FILE FORMAT IF NOT EXISTS (\S+)", rendered):
        assert f"GRANT OWNERSHIP ON FILE FORMAT {created} TO ROLE" in rendered, created


# --------------------------------------------------------------------------- #
# Lint rules
# --------------------------------------------------------------------------- #


def test_literal_object_name_is_rejected(deployer: Deployer, tmp_path: Path) -> None:
    bad = tmp_path / "bad.sql"
    bad.write_text("SELECT * FROM RISK_COPILOT.CURATED.T;", encoding="utf-8")
    with pytest.raises(ConfigError, match="literal object name"):
        deployer.render(bad)


def test_adjacent_string_literals_are_rejected(deployer: Deployer, tmp_path: Path) -> None:
    # Snowflake does not concatenate adjacent literals the way C does.
    bad = tmp_path / "bad.sql"
    bad.write_text("COMMENT ON TABLE t IS 'first part '\n    'second part';", encoding="utf-8")
    with pytest.raises(ConfigError, match="consecutive lines"):
        deployer.render(bad)


def test_undefined_template_variable_fails_loudly(deployer: Deployer, tmp_path: Path) -> None:
    # StrictUndefined matters: a silently-empty identifier would create the
    # wrong object rather than raising.
    bad = tmp_path / "bad.sql"
    bad.write_text("SELECT * FROM {{ nonexistent_var }}.T;", encoding="utf-8")
    with pytest.raises(ConfigError, match="template error"):
        deployer.render(bad)


def test_dry_run_reports_statement_counts_without_connecting(deployer: Deployer) -> None:
    result = deployer.run(stages=["ddl"], dry_run=True)
    assert result.entries
    assert not result.failed
    assert all(entry.statement_count > 0 for entry in result.entries)


def test_stage_selection_uses_canonical_order(deployer: Deployer) -> None:
    # A mis-ordered command line must not produce a broken deploy.
    result = deployer.run(stages=["ddl", "bootstrap"], dry_run=True)
    assert result.entries[0].stage == "bootstrap"
