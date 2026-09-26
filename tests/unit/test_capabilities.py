"""Tests for evidence-driven feature gating.

These assert the tri-state rule that makes the mechanism trustworthy: a
capability the probe never tested must never be read as available. A test that
only checked available/unavailable would pass while the module silently assumed
the very things this exercise set out to stop assuming.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from risk_copilot.capabilities import (
    DISABLE_AND_REPORT,
    FAIL,
    FEATURES,
    Capabilities,
    Feature,
    FeatureState,
    capabilities_path,
    load_capabilities,
    on_unavailable_policy,
)
from risk_copilot.config import Settings
from risk_copilot.doctor import Report, Status, check_features


def _capabilities(probes: dict[str, dict[str, Any]], **account: Any) -> Capabilities:
    return Capabilities(
        connection="TEST_CONN",
        probed_at="2026-09-23T00:00:00+00:00",
        account=account or {"edition": "STANDARD"},
        probes=probes,
    )


def _ok(*ids: str) -> dict[str, dict[str, Any]]:
    return {i: {"available": True} for i in ids}


def _blocked(probe_id: str, failure_class: str, code: str) -> dict[str, dict[str, Any]]:
    return {
        probe_id: {
            "available": False,
            "failure_class": failure_class,
            "error_code": code,
        }
    }


# --------------------------------------------------------------------------- #
# Tri-state availability
# --------------------------------------------------------------------------- #


def test_untested_probe_is_none_not_false() -> None:
    caps = _capabilities(_ok("CREATE_DYNAMIC_TABLE"))
    assert caps.available("CREATE_DYNAMIC_TABLE") is True
    assert caps.available("NEVER_PROBED") is None


def test_untested_requirement_yields_unknown_not_enabled() -> None:
    """The decisive case: absence of evidence must not read as evidence of presence."""
    caps = _capabilities(_ok("CREATE_DYNAMIC_TABLE"))
    status = caps.evaluate(
        "pipeline",
        Feature(
            description="d",
            requires_all=("CREATE_DYNAMIC_TABLE", "DYNAMIC_TABLE_REFRESHES"),
        ),
    )
    assert status.state is FeatureState.UNKNOWN
    assert status.enabled is False
    assert status.untested == ("DYNAMIC_TABLE_REFRESHES",)


def test_all_requirements_available_enables_feature() -> None:
    caps = _capabilities(_ok("A", "B"))
    status = caps.evaluate("f", Feature(description="d", requires_all=("A", "B")))
    assert status.state is FeatureState.ENABLED
    assert status.blockers == ()


def test_blocked_requirement_disables_and_keeps_the_cause() -> None:
    caps = _capabilities({**_ok("A"), **_blocked("B", "c_trial", "399258")})
    status = caps.evaluate("f", Feature(description="d", requires_all=("A", "B")))
    assert status.state is FeatureState.DISABLED
    assert [b.probe_id for b in status.blockers] == ["B"]
    assert status.blockers[0].failure_class == "c_trial"
    assert status.blockers[0].error_code == "399258"
    # The summary must name the cause, not merely report a loss.
    assert "c_trial" in status.summary()
    assert "399258" in status.summary()


def test_blocked_beats_untested_when_both_present() -> None:
    """A known blocker is a firmer answer than a gap, so it decides the state."""
    caps = _capabilities(_blocked("B", "b_edition", "000002"))
    status = caps.evaluate("f", Feature(description="d", requires_all=("B", "NEVER_PROBED")))
    assert status.state is FeatureState.DISABLED


# --------------------------------------------------------------------------- #
# requires_any
# --------------------------------------------------------------------------- #


def test_requires_any_enabled_when_one_is_available() -> None:
    caps = _capabilities({**_blocked("M1", "c_trial", "399258"), **_ok("M2")})
    status = caps.evaluate("f", Feature(description="d", requires_any=("M1", "M2")))
    assert status.state is FeatureState.ENABLED


def test_requires_any_disabled_when_all_are_blocked() -> None:
    caps = _capabilities(
        {**_blocked("M1", "c_trial", "399258"), **_blocked("M2", "c_trial", "399258")}
    )
    status = caps.evaluate("f", Feature(description="d", requires_any=("M1", "M2")))
    assert status.state is FeatureState.DISABLED
    assert {b.probe_id for b in status.blockers} == {"M1", "M2"}


def test_requires_any_unknown_when_none_were_probed() -> None:
    caps = _capabilities(_ok("UNRELATED"))
    status = caps.evaluate("f", Feature(description="d", requires_any=("M1", "M2")))
    assert status.state is FeatureState.UNKNOWN
    assert set(status.untested) == {"M1", "M2"}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_load_returns_none_when_no_evidence_exists(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_SNOWFLAKE_CONNECTION", "NO_SUCH_CONNECTION_XYZ")
    settings = Settings.load(repo_root=repo_root, profile="dev")
    assert load_capabilities(settings) is None


def test_load_round_trips_a_written_file(repo_root: Path, tmp_path: Path) -> None:
    settings = Settings.load(repo_root=repo_root, profile="dev")
    payload = {
        "connection": settings.connection_name,
        "probed_at": "2026-09-23T08:00:00+00:00",
        "account": {"edition": "ENTERPRISE"},
        "probes": {"CREATE_DYNAMIC_TABLE": {"available": True}},
    }
    path = capabilities_path(settings)
    existed = path.is_file()
    original = path.read_bytes() if existed else None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        caps = load_capabilities(settings)
        assert caps is not None
        assert caps.edition == "ENTERPRISE"
        assert caps.available("CREATE_DYNAMIC_TABLE") is True
    finally:
        # Never leave the real evidence file replaced by a fixture.
        if original is not None:
            path.write_bytes(original)
        else:
            path.unlink(missing_ok=True)


def test_malformed_file_is_none_rather_than_an_exception(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_SNOWFLAKE_CONNECTION", "MALFORMED_PROBE_TEST")
    settings = Settings.load(repo_root=repo_root, profile="dev")
    path = capabilities_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text("just a string, not a mapping", encoding="utf-8")
        assert load_capabilities(settings) is None
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def test_policy_comes_from_ml_yml(repo_root: Path) -> None:
    settings = Settings.load(repo_root=repo_root, profile="dev")
    assert on_unavailable_policy(settings) == DISABLE_AND_REPORT


def test_unrecognised_policy_falls_back_to_disable(repo_root: Path) -> None:
    """A typo in the policy must not silently become 'fail' at demo time."""
    settings = Settings.load(repo_root=repo_root, profile="dev")
    settings.ml["preconditions"]["on_unavailable"] = "shout_loudly"
    try:
        assert on_unavailable_policy(settings) == DISABLE_AND_REPORT
    finally:
        settings.ml["preconditions"]["on_unavailable"] = DISABLE_AND_REPORT


# --------------------------------------------------------------------------- #
# doctor integration
# --------------------------------------------------------------------------- #


def test_check_features_warns_when_no_evidence(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_SNOWFLAKE_CONNECTION", "NO_SUCH_CONNECTION_XYZ")
    settings = Settings.load(repo_root=repo_root, profile="dev")
    report = Report()
    assert check_features(settings, report) == {}
    evidence = [c for c in report.checks if c.name == "capability evidence"]
    assert len(evidence) == 1
    assert evidence[0].status is Status.WARN
    # The remedy must tell the developer how to produce the evidence.
    assert "probe_capabilities.py" in evidence[0].remedy


def test_check_features_disables_without_failing_under_default_policy(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disable_and_report means WARN, not FAIL: the build proceeds, reduced."""
    monkeypatch.setenv("RC_SNOWFLAKE_CONNECTION", "GATING_POLICY_TEST")
    settings = Settings.load(repo_root=repo_root, profile="dev")
    path = capabilities_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "connection": settings.connection_name,
        "probed_at": "2026-09-23T08:00:00+00:00",
        "account": {"edition": "STANDARD"},
        "probes": {
            "CREATE_DYNAMIC_TABLE": {"available": True},
            "DYNAMIC_TABLE_REFRESHES": {"available": True},
            "ROW_ACCESS_POLICY": {
                "available": False,
                "failure_class": "a_absent",
                "error_code": "000002",
            },
        },
    }
    try:
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        report = Report()
        enabled = check_features(settings, report)

        assert enabled["dynamic_table_pipeline"] is True
        assert enabled["row_level_security"] is False
        assert report.failed == []

        rls = next(c for c in report.checks if c.name == "row_level_security")
        assert rls.status is Status.WARN
        assert "a_absent" in rls.detail
        assert "Fallback:" in rls.remedy
    finally:
        path.unlink(missing_ok=True)


def test_check_features_fails_hard_when_policy_says_fail(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_SNOWFLAKE_CONNECTION", "FAIL_POLICY_TEST")
    settings = Settings.load(repo_root=repo_root, profile="dev")
    settings.ml["preconditions"]["on_unavailable"] = FAIL
    path = capabilities_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "connection": settings.connection_name,
        "probed_at": "2026-09-23T08:00:00+00:00",
        "account": {"edition": "STANDARD"},
        "probes": {
            "ROW_ACCESS_POLICY": {
                "available": False,
                "failure_class": "a_absent",
                "error_code": "000002",
            }
        },
    }
    try:
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        report = Report()
        check_features(settings, report)
        assert report.exit_code == 1
    finally:
        settings.ml["preconditions"]["on_unavailable"] = DISABLE_AND_REPORT
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Feature table coherence
# --------------------------------------------------------------------------- #


def test_every_feature_declares_at_least_one_requirement() -> None:
    """A feature with no requirements would always report enabled, proving nothing."""
    for name, spec in FEATURES.items():
        assert spec.requires_all or spec.requires_any, name


def test_every_feature_states_a_consequence() -> None:
    for name, spec in FEATURES.items():
        assert spec.description, name
        assert spec.fallback, f"{name} must state what the build does instead"


def test_critical_path_features_are_present() -> None:
    """These are the features the design cannot be reduced around."""
    for name in ("dynamic_table_pipeline", "signal_routing", "agent_tool_procedures"):
        assert name in FEATURES


def test_real_evidence_file_evaluates_without_error(repo_root: Path) -> None:
    """The committed evidence must satisfy the loader it was written for."""
    settings = Settings.load(repo_root=repo_root, profile="dev")
    caps = load_capabilities(settings)
    if caps is None:
        pytest.skip("No capability evidence committed for this connection.")
    statuses = caps.evaluate_all()
    assert set(statuses) == set(FEATURES)
    for name, status in statuses.items():
        assert status.name == name
        assert status.summary()
