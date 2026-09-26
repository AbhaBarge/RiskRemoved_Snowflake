"""Tests for retention clamping and the ML edition precondition.

Both cover bugs that were live in `conf/`, and both are the kind that surface
only against a real account:

* `conf/profiles/demo.yml` asks for 14 days of Time Travel. A Standard-edition
  account rejects anything above its cap at `CREATE SCHEMA` time with error
  001048, so **a demo-profile deploy would have failed outright**. The fix caps
  the value from recorded probe evidence rather than editing the profile, so the
  profile keeps stating its intent and an upgraded account honours it unchanged.
* `conf/ml.yml` required Enterprise before any ML feature was considered usable.
  The probe sweep records `SNOWFLAKE.ML.ANOMALY_DETECTION` as available on this
  Standard trial, so that precondition was **disabling a working feature**.

The clamp tests build a throwaway copy of `conf/` so they can fabricate probe
evidence without writing into the repository, then one test runs against the
real `conf/` and the real evidence file, because that is the regression that
actually broke.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from risk_copilot.capabilities import FEATURES, load_capabilities
from risk_copilot.config import capabilities_file, load_settings
from risk_copilot.doctor import Report, Status, check_config

pytestmark = pytest.mark.unit

#: Matches the probe id `config._RETENTION_PROBE_ID` looks for.
PROBE_ID = "RETENTION_14_DAYS"

#: Snowflake's actual rejection, which is where the cap is read from.
CAP_ERROR = (
    "001048 (22023): SQL compilation error: "
    "Exceeds maximum allowable retention time (1 day(s))."
)


@pytest.fixture
def fake_repo(repo_root: Path, tmp_path: Path) -> Path:
    """A copy of the real ``conf/`` tree, safe to fabricate evidence inside."""
    shutil.copytree(repo_root / "conf", tmp_path / "conf")
    # Remove any real evidence so each test states its own starting point
    # explicitly rather than inheriting whatever the last probe recorded.
    generated = tmp_path / "conf" / "generated"
    for path in generated.glob("capabilities.*.yml"):
        path.unlink()
    return tmp_path


def write_evidence(repo: Path, connection: str, entry: dict[str, Any], **account: Any) -> Path:
    path = capabilities_file(repo, connection)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "connection": connection,
                "probed_at": "2026-09-23",
                "account": {"edition": "STANDARD", **account},
                "probes": {PROBE_ID: entry},
            }
        ),
        encoding="utf-8",
    )
    return path


def connection_name(repo: Path) -> str:
    external = yaml.safe_load((repo / "conf" / "external.yml").read_text(encoding="utf-8"))
    return str(external["snowflake"]["connection_name"])


# --------------------------------------------------------------------------- #
# The clamp
# --------------------------------------------------------------------------- #


def test_cap_is_read_from_the_accounts_own_rejection_message(fake_repo: Path) -> None:
    """The cap comes from Snowflake stating its limit, not from an assumption."""
    write_evidence(
        fake_repo,
        connection_name(fake_repo),
        {"available": False, "failure_class": "b_edition", "error_code": "001048",
         "error": CAP_ERROR},
    )

    settings = load_settings(repo_root=fake_repo, profile="demo")

    assert settings.retention.landing_days == 1
    assert settings.retention.evidence_days == 1
    clamp = settings.retention_clamp
    assert clamp is not None
    assert clamp.max_days == 1
    assert clamp.cap_source == "probe error message"
    # The reason travels with the decision: a clamp without the failure class and
    # error code is the kind of unexplained claim this project removes.
    assert clamp.requested == {"landing_days": 14, "evidence_days": 14, "default_days": 1}
    assert clamp.failure_class == "b_edition"
    assert clamp.error_code == "001048"
    assert "14->1" in clamp.summary()


def test_cap_falls_back_to_the_account_retention_parameter(fake_repo: Path) -> None:
    """A rejection without the cap in its text still yields a usable ceiling.

    On an edition that caps retention, the account default and the cap coincide,
    so the parameter is a sound fallback -- but only once the probe has proven
    that extended retention is refused.
    """
    write_evidence(
        fake_repo,
        connection_name(fake_repo),
        {"available": False, "failure_class": "b_edition", "error": "no cap named here"},
        param_data_retention_time_in_days="3",
    )

    settings = load_settings(repo_root=fake_repo, profile="demo")

    assert settings.retention.landing_days == 3
    assert settings.retention_clamp is not None
    assert settings.retention_clamp.cap_source == "account DATA_RETENTION_TIME_IN_DAYS"


def test_no_clamp_when_the_account_allows_the_requested_retention(fake_repo: Path) -> None:
    write_evidence(fake_repo, connection_name(fake_repo), {"available": True})

    settings = load_settings(repo_root=fake_repo, profile="demo")

    assert settings.retention.landing_days == 14
    assert settings.retention_clamp is None


def test_no_clamp_without_evidence(fake_repo: Path) -> None:
    """Absent evidence must not be read as a constraint.

    The mirror of `capabilities.FeatureState.UNKNOWN`: an untested capability is
    not a measured one, in either direction.
    """
    settings = load_settings(repo_root=fake_repo, profile="demo")

    assert settings.retention.landing_days == 14
    assert settings.retention_clamp is None


def test_unestablishable_cap_leaves_the_value_alone(fake_repo: Path) -> None:
    """Knowing 14 is refused is not knowing what is allowed.

    Rather than guess a ceiling, the configured value stands and the deploy fails
    with Snowflake's own error -- a loud, accurate failure beats deploying a
    number nobody chose.
    """
    write_evidence(
        fake_repo,
        connection_name(fake_repo),
        {"available": False, "failure_class": "b_edition", "error": "refused, no cap stated"},
    )

    settings = load_settings(repo_root=fake_repo, profile="demo")

    assert settings.retention.landing_days == 14
    assert settings.retention_clamp is None


def test_clamp_is_exposed_to_sql_rendering(fake_repo: Path) -> None:
    """A rendered DDL header can explain why the deployed value differs."""
    write_evidence(
        fake_repo,
        connection_name(fake_repo),
        {"available": False, "failure_class": "b_edition", "error_code": "001048",
         "error": CAP_ERROR},
    )

    context = load_settings(repo_root=fake_repo, profile="demo").sql_context()

    assert context["retention"]["landing_days"] == 1
    assert context["retention_clamp"]["max_days"] == 1
    assert context["retention_clamp"]["probe_id"] == PROBE_ID


def test_sql_context_retention_clamp_is_null_when_nothing_was_clamped(repo_root: Path) -> None:
    context = load_settings(repo_root=repo_root, profile="dev").sql_context()

    assert context["retention_clamp"] is None


# --------------------------------------------------------------------------- #
# The regression, against the real configuration
# --------------------------------------------------------------------------- #


def test_demo_profile_retention_is_deployable_against_the_recorded_account(
    repo_root: Path,
) -> None:
    """The bug itself: a demo deploy must not exceed the measured account cap.

    Skipped when no evidence has been recorded for the configured connection --
    there is then nothing to assert, and a skip says that honestly rather than
    passing vacuously.
    """
    settings = load_settings(repo_root=repo_root, profile="demo")
    capabilities = load_capabilities(settings)
    if capabilities is None or capabilities.available(PROBE_ID) is not False:
        pytest.skip(f"No recorded {PROBE_ID} failure for {settings.connection_name!r}")

    clamp = settings.retention_clamp
    assert clamp is not None, (
        "demo.yml requests extended Time Travel that this account refuses (001048). "
        "Without a clamp, `deploy --stage bootstrap` fails at CREATE SCHEMA."
    )
    for key, value in settings.retention.model_dump().items():
        assert value <= clamp.max_days, f"retention.{key} exceeds the account cap"


def test_doctor_reports_the_clamp_rather_than_applying_it_silently(fake_repo: Path) -> None:
    write_evidence(
        fake_repo,
        connection_name(fake_repo),
        {"available": False, "failure_class": "b_edition", "error_code": "001048",
         "error": CAP_ERROR},
    )
    settings = load_settings(repo_root=fake_repo, profile="demo")

    report = Report()
    check_config(settings, report)

    clamped = [c for c in report.checks if c.name == "retention clamped"]
    assert len(clamped) == 1
    assert clamped[0].status is Status.WARN
    assert "001048" in clamped[0].detail
    # A warning must not fail the build: correctness does not depend on Time
    # Travel (Gap 15), only the demonstrable provenance window does.
    assert report.exit_code == 0


def test_doctor_reports_plain_retention_when_nothing_is_clamped(repo_root: Path) -> None:
    report = Report()
    check_config(load_settings(repo_root=repo_root, profile="dev"), report)

    names = {c.name for c in report.checks}
    assert "retention" in names
    assert "retention clamped" not in names


# --------------------------------------------------------------------------- #
# The ML edition precondition
# --------------------------------------------------------------------------- #


def test_ml_preconditions_do_not_gate_on_edition(repo_root: Path) -> None:
    """Edition is the wrong gate for SNOWFLAKE.ML functions.

    `require_edition: [ENTERPRISE, BUSINESS_CRITICAL]` made `doctor` warn on a
    Standard account and disabled `tier1.velocity_anomaly`, which the probe sweep
    records as available. Availability is decided per function by evidence, never
    inferred from the edition string.
    """
    settings = load_settings(repo_root=repo_root, profile="demo")
    preconditions = settings.ml["preconditions"]

    assert preconditions["require_edition"] == []
    assert preconditions["on_unavailable"] == "disable_and_report"


def test_velocity_anomaly_is_enabled_in_the_ml_tier(repo_root: Path) -> None:
    settings = load_settings(repo_root=repo_root, profile="demo")

    assert settings.ml["tier1"]["velocity_anomaly"]["enabled"] is True


def test_regionally_absent_ml_functions_say_so_in_future_scope(repo_root: Path) -> None:
    """CLASSIFICATION and TOP_INSIGHTS are regional, not trial or edition.

    The distinction is load-bearing for the "what production would need" slide:
    converting the account fixes the trial-gated features and does nothing for
    these two.
    """
    future = load_settings(repo_root=repo_root, profile="demo").ml["future_scope"]

    for key in ("triage_classifier", "top_insights"):
        assert "REGION" in future[key]["why_deferred"].upper(), key


def test_velocity_anomaly_feature_is_enabled_by_the_recorded_evidence(repo_root: Path) -> None:
    """End-to-end: the working feature is no longer reported as unavailable."""
    settings = load_settings(repo_root=repo_root, profile="dev")
    capabilities = load_capabilities(settings)
    if capabilities is None:
        pytest.skip(f"No capability evidence for {settings.connection_name!r}")
    if capabilities.available("SNOWFLAKE.ML.ANOMALY_DETECTION") is not True:
        pytest.skip("This account does not serve SNOWFLAKE.ML.ANOMALY_DETECTION")

    status = capabilities.evaluate("velocity_anomaly_ml", FEATURES["velocity_anomaly_ml"])

    assert status.enabled, status.summary()
