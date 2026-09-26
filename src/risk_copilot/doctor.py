"""Preflight diagnostics.

``riskcopilot doctor`` answers one question: *is this environment capable of the
next step?* It runs before generation, before deploy, and before the demo,
because every failure it reports is one that would otherwise surface halfway
through creating objects or -- worse -- on stage.

Checks are grouped and each is independent: a failing group never prevents the
others from reporting, so one run gives the full picture rather than the first
problem.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from risk_copilot.capabilities import (
    DISABLE_AND_REPORT,
    FAIL,
    Capabilities,
    capabilities_path,
    load_capabilities,
    on_unavailable_policy,
)
from risk_copilot.config import Settings, data_manifest_file
from risk_copilot.session import SessionError, build_connect_params, execute, fetch_scalar

logger = logging.getLogger(__name__)


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(slots=True)
class Check:
    """One diagnostic result."""

    group: str
    name: str
    status: Status
    detail: str = ""
    remedy: str = ""


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(
        self,
        group: str,
        name: str,
        status: Status,
        detail: str = "",
        remedy: str = "",
    ) -> None:
        self.checks.append(Check(group, name, status, detail, remedy))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status is Status.WARN]

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def check_config(settings: Settings, report: Report) -> None:
    """Confirm the configuration resolved and is internally consistent.

    Reaching this function already proves a great deal: ``Settings.load``
    validates identifiers, target lags and cross-section consistency, and raises
    otherwise.
    """
    g = "config"
    report.add(g, "profile", Status.OK, f"{settings.profile} (suffix {settings.object_suffix!r})")
    report.add(g, "database", Status.OK, settings.db())
    report.add(
        g,
        "warehouses",
        Status.OK,
        ", ".join(f"{settings.obj(w.name)}={w.size}" for w in settings.warehouses.values()),
    )

    # Retention is reduced when the account permits less than the profile asks
    # for. Reported rather than silent: the deploy succeeds, but the Time Travel
    # provenance beat is narrower than the profile intended, and that is exactly
    # the kind of quiet degradation this whole diagnostic exists to surface.
    if settings.retention_clamp is not None:
        report.add(
            g,
            "retention clamped",
            Status.WARN,
            f"Time Travel reduced to what the account allows: "
            f"{settings.retention_clamp.summary()}",
            "The deploy will succeed. EVIDENCE_QUERY_LOG.result_snapshot remains "
            "the Reviewer's source of truth (Gap 15), so correctness is "
            "unaffected -- only the demonstrable Time Travel window shrinks. "
            "Raise the account edition to honour the profile as written.",
        )
    else:
        report.add(
            g,
            "retention",
            Status.OK,
            ", ".join(f"{k}={v}" for k, v in settings.retention.model_dump().items()),
        )

    regions = sorted(settings.roles.branch_analysts)
    report.add(
        g,
        "rls demo roles",
        Status.OK,
        f"{len(regions)} branch analyst role(s): {', '.join(regions)}",
    )

    # A single analyst role cannot demonstrate differential access.
    if len(regions) < 2:
        report.add(
            g,
            "rls demo coverage",
            Status.WARN,
            "Only one branch analyst role is configured.",
            "The 'same question, two roles, two answers' beat needs at least two "
            "regions in conf/base.yml:roles.branch_analysts.",
        )

    # Thresholds must be traceable to a real source, or the demo risks citing a
    # regulation that does not exist.
    clauses = settings.detection.get("clauses") or []
    missing = [
        c.get("clause_id", "<unnamed>")
        for c in clauses
        if not (c.get("provenance") or {}).get("source_document")
    ]
    if missing:
        report.add(
            g,
            "policy provenance",
            Status.FAIL,
            f"{len(missing)} clause(s) cite no source document: {missing}",
            "Every clause in conf/detection.yml needs provenance.source_document. "
            "This is what keeps the demo from citing a hallucinated circular.",
        )
    else:
        report.add(g, "policy provenance", Status.OK, f"{len(clauses)} clauses, all sourced")

    # Detection SQL resolves thresholds from POLICY_CLAUSE; a threshold that
    # exists only in YAML would be a hardcoded value in disguise.
    ml_tier1 = settings.ml.get("tier1") or {}
    fuzzy = ml_tier1.get("fuzzy_sanctions") or {}
    referenced = {
        str(value)
        for value in (
            fuzzy.get("exact_match_clause_id"),
            fuzzy.get("review_floor_clause_id"),
        )
        if value is not None
    }
    known = {str(c.get("clause_id")) for c in clauses if c.get("clause_id") is not None}
    if dangling := referenced - known:
        report.add(
            g,
            "clause references",
            Status.FAIL,
            f"conf/ml.yml references unknown clause id(s): {sorted(dangling)}",
            "Add the clause to conf/detection.yml or correct the reference.",
        )


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #


def check_data(settings: Settings, report: Report, *, verify_checksums: bool = False) -> None:
    """Verify source datasets against ``data/MANIFEST.yml``.

    Checksums are skipped by default: hashing ~950 MB takes minutes, which is
    too slow for a routine preflight. ``--checksums`` forces it, and should be
    run once after acquiring the files.
    """
    g = "data"
    manifest_path = data_manifest_file(settings.repo_root)
    if not manifest_path.is_file():
        report.add(g, "manifest", Status.FAIL, f"Not found: {manifest_path}")
        return

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    datasets: dict[str, dict[str, Any]] = manifest.get("datasets") or {}
    report.add(g, "manifest", Status.OK, f"{len(datasets)} datasets declared")

    for name, spec in datasets.items():
        required = bool(spec.get("required"))
        rel = str(spec.get("path", ""))
        path = settings.repo_root / rel

        if not path.is_file():
            report.add(
                g,
                name,
                Status.FAIL if required else Status.SKIP,
                f"Missing: {rel}",
                f"Fetch from {spec.get('url') or spec.get('source', 'the documented source')}",
            )
            continue

        actual_bytes = path.stat().st_size
        expected_bytes = spec.get("bytes")
        if expected_bytes and actual_bytes != expected_bytes:
            report.add(
                g,
                name,
                Status.FAIL,
                f"Size mismatch: expected {expected_bytes:,} bytes, found {actual_bytes:,}",
                "The file is truncated or a different revision. Re-download it.",
            )
            continue

        if not verify_checksums or not spec.get("sha256"):
            report.add(g, name, Status.OK, f"{actual_bytes:,} bytes (checksum not verified)")
            continue

        digest = _sha256(path)
        if digest == str(spec["sha256"]).lower():
            report.add(g, name, Status.OK, f"{actual_bytes:,} bytes, sha256 verified")
        else:
            report.add(
                g,
                name,
                Status.FAIL,
                f"Checksum mismatch\n  expected {spec['sha256']}\n  actual   {digest}",
                "The file differs from the one this build was validated against. "
                "Re-download, or update data/MANIFEST.yml if the change is intended.",
            )


def _sha256(path: Path, chunk_size: int = 1 << 22) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def check_connection(settings: Settings, report: Report) -> Any | None:
    """Verify the Snowflake connection resolves and authenticates.

    Returns an open session for the later checks to reuse, or ``None``. The
    caller owns closing it.
    """
    g = "connection"
    try:
        params = build_connect_params(settings)
    except SessionError as exc:
        report.add(g, "connection config", Status.FAIL, str(exc))
        return None

    report.add(
        g,
        "connection config",
        Status.OK,
        f"{settings.connection_name} (account={params.get('account', '?')}, "
        f"authenticator={params.get('authenticator', 'snowflake')})",
    )

    if not os.environ.get("RC_SNOWFLAKE_PRIVATE_KEY_PATH"):
        report.add(
            g,
            "unattended auth",
            Status.WARN,
            "No key-pair configured.",
            "Automations and CI cannot complete an interactive login. Generate a "
            "key pair, register the public key on the user, and set "
            "RC_SNOWFLAKE_PRIVATE_KEY_PATH.",
        )

    try:
        # Opened outside the context manager so later checks can reuse it, and
        # via create_session so it inherits the pre-bootstrap fallback: the
        # configured role does not exist until the bootstrap stage has run.
        from risk_copilot.session import create_session

        session = create_session(settings)
    except Exception as exc:
        from risk_copilot.session import _explain_connection_failure

        message = (
            str(exc)
            if isinstance(exc, SessionError)
            else _explain_connection_failure(exc, settings)
        )
        report.add(g, "authenticate", Status.FAIL, message)
        return None

    try:
        who = session.sql(
            "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_ACCOUNT(), CURRENT_REGION()"
        ).collect()[0]
        report.add(
            g,
            "authenticate",
            Status.OK,
            f"user={who[0]} role={who[1]} account={who[2]} region={who[3]}",
        )
    except Exception as exc:
        report.add(g, "authenticate", Status.FAIL, str(exc))
        session.close()
        return None

    return session


# --------------------------------------------------------------------------- #
# Account capabilities
# --------------------------------------------------------------------------- #


def _detect_edition(session: Any) -> str | None:
    """Return the account edition, or ``None`` if it cannot be read.

    There is no ``CURRENT_EDITION()`` function; the edition is only exposed as a
    column. ``ORGANIZATION_USAGE.ACCOUNTS`` is tried first and matched on the
    account locator, then ``SHOW ORGANIZATION ACCOUNTS`` as a fallback, since
    each needs privileges the other does not. A role without either still gets a
    warning rather than a failure -- the edition gates one optional ML signal,
    not the build.
    """
    try:
        locator = fetch_scalar(session, "SELECT CURRENT_ACCOUNT()")
    except SessionError:
        return None

    try:
        edition = fetch_scalar(
            session,
            "SELECT edition FROM snowflake.organization_usage.accounts "
            "WHERE account_locator = ? AND deleted_on IS NULL "
            "ORDER BY created_on DESC LIMIT 1",
            [locator],
        )
        if edition:
            return str(edition)
    except SessionError:
        logger.debug("ORGANIZATION_USAGE.ACCOUNTS edition lookup failed", exc_info=True)

    try:
        for row in execute(session, "SHOW ORGANIZATION ACCOUNTS"):
            mapping = row.as_dict()
            if mapping.get("account_locator") == locator:
                return str(mapping.get("edition")) if mapping.get("edition") else None
    except (SessionError, AttributeError):
        logger.debug("SHOW ORGANIZATION ACCOUNTS edition lookup failed", exc_info=True)

    return None


def check_capabilities(settings: Settings, session: Any, report: Report) -> None:
    """Confirm edition and the built-in functions the ML tier depends on.

    ML features are edition- and region-gated. Discovering that at demo time is
    unacceptable, so an unmet precondition disables the feature and says so --
    ``conf/ml.yml:preconditions.on_unavailable`` records that policy.
    """
    g = "capabilities"

    edition = _detect_edition(session)

    required = {
        e.upper() for e in (settings.ml.get("preconditions") or {}).get("require_edition", [])
    }
    if edition is None:
        report.add(g, "edition", Status.WARN, "Could not determine the account edition.")
    elif not required or str(edition).upper() in required:
        report.add(g, "edition", Status.OK, str(edition))
    else:
        report.add(
            g,
            "edition",
            Status.WARN,
            f"Edition is {edition}; ML features expect one of {sorted(required)}.",
            "Tier-1 detection rules still work. The velocity-anomaly signal will "
            "be disabled and reported as such.",
        )

    # Probe each function with a minimal call. SQL compilation is the honest
    # test of availability -- a name in the documentation is not.
    probes = {
        "JAROWINKLER_SIMILARITY": "SELECT JAROWINKLER_SIMILARITY('kumar', 'kumaar')",
        "EDITDISTANCE": "SELECT EDITDISTANCE('kumar', 'kumaar')",
        "SOUNDEX": "SELECT SOUNDEX('kumar')",
        "AI_COMPLETE": "SELECT AI_COMPLETE('llama3.1-8b', 'ok')",
    }
    for name in (settings.ml.get("preconditions") or {}).get("verify_functions", []):
        sql = probes.get(str(name))
        if sql is None:
            # Table functions such as SNOWFLAKE.ML.ANOMALY_DETECTION cannot be
            # probed with a scalar SELECT; checked at deploy time instead.
            report.add(g, str(name), Status.SKIP, "Verified at ML deploy time, not here.")
            continue
        try:
            fetch_scalar(session, sql)
            report.add(g, str(name), Status.OK, "available")
        except SessionError as exc:
            report.add(
                g,
                str(name),
                Status.WARN,
                f"Unavailable: {str(exc).splitlines()[-1][:160]}",
                "The dependent feature will be disabled rather than failing at runtime.",
            )


def check_features(settings: Settings, report: Report) -> dict[str, bool]:
    """Resolve which build features are usable, from recorded probe evidence.

    This is the mechanism that makes the build auto-disable rather than fail at
    runtime. It needs no connection: the evidence was gathered by
    ``scripts/probe_capabilities.py`` and committed, so the same answers are
    available offline and in CI.

    Returns:
        Mapping of feature name to enabled/disabled, for a caller that wants to
        gate deployment on it.
    """
    g = "features"
    policy = on_unavailable_policy(settings)
    capabilities = load_capabilities(settings)

    if capabilities is None:
        report.add(
            g,
            "capability evidence",
            Status.WARN,
            f"No capability evidence for connection {settings.connection_name!r}.",
            f"Run: python scripts/probe_capabilities.py --profile {settings.profile}\n"
            f"Expected at {capabilities_path(settings).name}. Until it exists, every "
            "feature is treated as unestablished rather than assumed available.",
        )
        return {}

    report.add(
        g,
        "capability evidence",
        Status.OK,
        f"{len(capabilities.probes)} probes recorded {capabilities.probed_at} "
        f"(edition={capabilities.edition or 'unknown'}, policy={policy})",
    )

    statuses = capabilities.evaluate_all()
    enabled: dict[str, bool] = {}

    for name, status in statuses.items():
        enabled[name] = status.enabled
        if status.enabled:
            report.add(g, name, Status.OK, status.description)
            continue

        # An unmet precondition is reported, not fatal, unless the configured
        # policy says otherwise. conf/ml.yml already records that choice.
        level = Status.FAIL if policy == FAIL else Status.WARN
        remedy = f"Fallback: {status.fallback}" if status.fallback else ""
        report.add(g, name, level, f"{status.description} -- {status.summary()}", remedy)

    disabled = sorted(n for n, ok in enabled.items() if not ok)
    if disabled and policy == DISABLE_AND_REPORT:
        report.add(
            g,
            "gating outcome",
            Status.OK,
            f"{len(enabled) - len(disabled)}/{len(enabled)} features enabled. "
            f"Disabled and reported: {', '.join(disabled)}",
        )

    return enabled


def _capability_note(capabilities: Capabilities | None, probe_id: str) -> str:
    """Render one probe's recorded outcome for a diagnostic line."""
    if capabilities is None:
        return "no evidence"
    entry = capabilities.probes.get(probe_id)
    if entry is None:
        return "not probed"
    if entry.get("available"):
        return "available"
    parts = [str(entry.get("failure_class") or "unclassified")]
    if entry.get("error_code"):
        parts.append(str(entry["error_code"]))
    return f"unavailable [{' '.join(parts)}]"


def check_models(settings: Settings, session: Any, report: Report) -> dict[str, str]:
    """Resolve each agent role to a model the account actually serves.

    The architecture pinned ``claude-3.5-sonnet`` and ``llama3.1-70b``. Model
    names change and availability is regional, so each role declares an ordered
    candidate list and the first that responds wins. The resolution is written
    to ``conf/generated/model_availability.yml`` and the deployer refuses to
    register an agent whose model was never confirmed.
    """
    g = "models"
    resolved: dict[str, str] = {}
    capabilities = load_capabilities(settings)

    for role in settings.models.get("roles") or {}:
        for candidate in settings.model_for(role):
            # Skip a candidate the probe already recorded as unavailable. This is
            # not just a saving: re-probing 28 models against a trial account
            # produces 28 identical 399258 errors and buries the real answer.
            if capabilities is not None:
                recorded = capabilities.available(f"AI_COMPLETE:{candidate}")
                if recorded is False:
                    continue
            try:
                fetch_scalar(
                    session,
                    "SELECT AI_COMPLETE(?, 'reply with the single word: ok')",
                    [candidate],
                )
            except SessionError:
                continue
            resolved[role] = candidate
            break

        if role in resolved:
            first = settings.model_for(role)[0]
            if resolved[role] == first:
                report.add(g, role, Status.OK, resolved[role])
            else:
                report.add(
                    g,
                    role,
                    Status.WARN,
                    f"Fell back to {resolved[role]} (preferred {first} is unavailable).",
                )
        else:
            candidates = settings.model_for(role)
            evidence = ", ".join(
                f"{c}={_capability_note(capabilities, f'AI_COMPLETE:{c}')}" for c in candidates[:4]
            )
            report.add(
                g,
                role,
                Status.FAIL,
                f"No candidate available: {candidates}\n  {evidence}",
                "Update the candidate list in conf/models.yml for this region, or "
                "target an account where Cortex is not gated. Features needing a "
                "model are disabled and reported rather than failing at runtime.",
            )

    if resolved:
        out = settings.repo_root / "conf" / "generated" / "model_availability.yml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            yaml.safe_dump(
                {
                    "resolved_on_profile": settings.profile,
                    "connection": settings.connection_name,
                    "models": resolved,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        report.add(g, "recorded", Status.OK, str(out.relative_to(settings.repo_root)))

    return resolved


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(
    settings: Settings,
    *,
    include_data: bool = True,
    include_connection: bool = True,
    include_models: bool = False,
    include_features: bool = True,
    verify_checksums: bool = False,
) -> Report:
    """Run the selected diagnostic groups and return a consolidated report."""
    report = Report()
    check_config(settings, report)

    if include_data:
        check_data(settings, report, verify_checksums=verify_checksums)

    # Feature gating reads committed evidence, so it runs before -- and
    # independently of -- the connection. A developer with no network still
    # learns which features this target account supports.
    if include_features:
        check_features(settings, report)

    if not include_connection:
        return report

    session = check_connection(settings, report)
    if session is None:
        # Offline work is still possible; say so rather than implying the build
        # is blocked.
        report.add(
            "connection",
            "offline capability",
            Status.WARN,
            "Config validation, data generation and SQL rendering do not need a "
            "connection; deploy and load do.",
        )
        return report

    try:
        check_capabilities(settings, session, report)
        if include_models:
            check_models(settings, session, report)
    finally:
        session.close()

    return report
