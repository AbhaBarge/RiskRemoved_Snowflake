"""Tests for the layered configuration loader.

Three properties matter enough to pin down:

* **Layering resolves in the documented order.** A profile overlay must win over
  the base, and an environment variable over both.
* **Invalid configuration fails at load time.** A bad identifier reaching the
  DDL renderer would be an injection vector; a bad target lag would fail
  mid-deploy.
* **Cross-section consistency is enforced.** The failure this catches -- an
  analyst role for a region generation never produces -- would otherwise show up
  as an *empty* row-access-policy demo that looks like working access control.
"""

from __future__ import annotations

import pytest

from risk_copilot.config import (
    ConfigError,
    Settings,
    deep_merge,
    load_settings,
    validate_identifier,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# deep_merge
# --------------------------------------------------------------------------- #


def test_deep_merge_recurses_into_nested_mappings() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    overlay = {"a": {"c": 99}}
    assert deep_merge(base, overlay) == {"a": {"b": 1, "c": 99}, "d": 3}


def test_deep_merge_replaces_lists_wholesale() -> None:
    # Deliberate: a profile that narrows branch_regions must be able to shorten
    # the list, which appending would make impossible.
    assert deep_merge({"x": [1, 2, 3]}, {"x": [9]}) == {"x": [9]}


def test_deep_merge_does_not_mutate_its_inputs() -> None:
    base = {"a": {"b": 1}}
    deep_merge(base, {"a": {"b": 2}})
    assert base == {"a": {"b": 1}}


# --------------------------------------------------------------------------- #
# Identifier validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["RISK_COPILOT", "_private", "T1", "a$b"])
def test_validate_identifier_accepts_and_uppercases(value: str) -> None:
    assert validate_identifier(value, what="Test") == value.upper()


@pytest.mark.parametrize(
    "value",
    [
        "1TABLE",  # leading digit
        "has space",
        "drop;table",  # statement injection
        "quote'd",
        "dash-name",
        "",
    ],
)
def test_validate_identifier_rejects_unsafe_values(value: str) -> None:
    # These values are interpolated into DDL, so rejection -- not quoting -- is
    # the correct response.
    with pytest.raises(ConfigError):
        validate_identifier(value, what="Test")


# --------------------------------------------------------------------------- #
# Loading and layering
# --------------------------------------------------------------------------- #


def test_dev_profile_applies_object_suffix(repo_root) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    assert settings.object_suffix == "_DEV"
    assert settings.db().endswith("_DEV")
    assert settings.fq("curated").endswith(".CURATED")
    assert settings.fq("curated", "DT_FOO") == f"{settings.db()}.CURATED.DT_FOO_DEV"


def test_demo_profile_uses_unsuffixed_names(repo_root) -> None:
    settings = load_settings(repo_root=repo_root, profile="demo")
    assert settings.object_suffix == ""
    assert settings.db() == "RISK_COPILOT"


def test_profile_overlay_wins_over_base(repo_root) -> None:
    # base.yml sets the fast signal lag to 5 minutes; dev.yml relaxes it to 60
    # so a development refresh does not burn credits every five minutes.
    dev = load_settings(repo_root=repo_root, profile="dev")
    demo = load_settings(repo_root=repo_root, profile="demo")
    assert dev.dt("signals_fast").target_lag == "60 minutes"
    assert demo.dt("signals_fast").target_lag == "5 minutes"


def test_domain_overrides_apply_to_domain_config(repo_root) -> None:
    # dev.yml:generation_overrides shrinks the sample for a fast build loop.
    dev = load_settings(repo_root=repo_root, profile="dev")
    demo = load_settings(repo_root=repo_root, profile="demo")
    assert dev.generation["subgraph"]["target_parties"] == 500
    assert demo.generation["subgraph"]["target_parties"] == 5000
    # Un-overridden keys must survive the merge.
    assert "fx" in dev.generation
    assert dev.generation["fx"]["base_currency"] == "INR"


def test_environment_variable_overrides_profile(repo_root, monkeypatch) -> None:
    monkeypatch.setenv("RC_DATABASE", "SCRATCH_DB")
    settings = load_settings(repo_root=repo_root, profile="dev")
    assert settings.db() == "SCRATCH_DB_DEV"


def test_seed_override_is_coerced_to_int(repo_root, monkeypatch) -> None:
    monkeypatch.setenv("RC_SEED", "12345")
    settings = load_settings(repo_root=repo_root, profile="dev")
    assert settings.generation["seed"] == 12345


def test_non_integer_seed_is_rejected(repo_root, monkeypatch) -> None:
    monkeypatch.setenv("RC_SEED", "not-a-number")
    with pytest.raises(ConfigError, match="RC_SEED"):
        load_settings(repo_root=repo_root, profile="dev")


def test_unknown_profile_is_rejected(repo_root) -> None:
    with pytest.raises(ConfigError, match="Unknown profile"):
        load_settings(repo_root=repo_root, profile="production")


# --------------------------------------------------------------------------- #
# Cross-section consistency
# --------------------------------------------------------------------------- #


def test_analyst_role_without_generated_region_is_rejected(repo_root) -> None:
    # The important failure mode: if generation never produces KOLKATA, the
    # row-access-policy demo returns an empty result that is indistinguishable
    # from correctly-filtered output.
    with pytest.raises(ConfigError, match="regions that generation never"):
        Settings.load(
            repo_root=repo_root,
            profile="demo",
            overrides={"roles": {"branch_analysts": {"KOLKATA": "BRANCH_ANALYST_KOLKATA"}}},
        )


def test_invalid_target_lag_is_rejected(repo_root) -> None:
    with pytest.raises(ConfigError, match="TARGET_LAG"):
        Settings.load(
            repo_root=repo_root,
            profile="demo",
            overrides={"dynamic_tables": {"signals_fast": {"name": "DT_X", "target_lag": "soon"}}},
        )


def test_downstream_target_lag_is_accepted(repo_root) -> None:
    settings = load_settings(repo_root=repo_root, profile="demo")
    assert settings.dt("txn_daily_agg").target_lag == "DOWNSTREAM"


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #


def test_unknown_schema_key_names_the_valid_options(repo_root) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    with pytest.raises(ConfigError, match="Unknown schema key"):
        settings.fq("nonexistent")


def test_model_for_returns_an_ordered_candidate_list(repo_root) -> None:
    # A list, not a single name: model availability is region-dependent, so the
    # doctor picks the first candidate the account actually serves.
    settings = load_settings(repo_root=repo_root, profile="dev")
    candidates = settings.model_for("reviewer")
    assert isinstance(candidates, list)
    assert len(candidates) >= 2


def test_reviewer_and_drafter_prefer_different_model_families(repo_root) -> None:
    # A critic that shares the drafter's blind spots is not a critic.
    settings = load_settings(repo_root=repo_root, profile="dev")
    assert settings.model_for("reviewer")[0] != settings.model_for("drafter")[0]


def test_settings_are_immutable(repo_root) -> None:
    settings = load_settings(repo_root=repo_root, profile="dev")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        settings.database = "OTHER"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# SQL context -- the contract the templates depend on
# --------------------------------------------------------------------------- #


def test_sql_context_exposes_every_identifier_templates_need(repo_root) -> None:
    context = load_settings(repo_root=repo_root, profile="dev").sql_context()
    for key in (
        "db",
        "schema",
        "fq",
        "wh",
        "wh_spec",
        "role",
        "stage",
        "retention",
        "dt",
        "view",
        "cortex",
    ):
        assert key in context, f"sql_context is missing {key!r}; sql/README.md documents it"


def test_sql_context_applies_the_suffix_everywhere(repo_root) -> None:
    # A single un-suffixed name would let a dev deploy overwrite a demo object.
    context = load_settings(repo_root=repo_root, profile="dev").sql_context()
    assert context["db"].endswith("_DEV")
    assert context["role"]["admin"].endswith("_DEV")
    assert context["wh"]["pipeline"].endswith("_DEV")
    assert context["cortex"]["agent"].endswith("_DEV")
    for name in context["dt"].values():
        assert name["name"].endswith("_DEV")
