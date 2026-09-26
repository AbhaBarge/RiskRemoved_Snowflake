"""Evidence-driven feature gating.

``scripts/probe_capabilities.py`` establishes what an account can actually do and
writes ``conf/generated/capabilities.<connection>.yml``. This module turns that
evidence into build decisions, so a feature whose dependency is unavailable is
**disabled and reported** rather than failing halfway through a deploy or, worse,
on stage.

The policy is not invented here. ``conf/ml.yml:preconditions.on_unavailable``
already records it as ``disable_and_report``, and that same contract is applied
to every feature in :data:`FEATURES`.

Two rules keep this honest:

* A capability the probe never tested is **unknown**, not available. Unknown
  disables the feature exactly as unavailable does, because building on an
  untested assumption is the failure mode this whole exercise exists to remove.
* The reason is always carried through. "Disabled" without a failure class and an
  error code is the kind of claim this module is meant to replace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from risk_copilot.config import Settings, capabilities_file

logger = logging.getLogger(__name__)

#: Policy values accepted by ``preconditions.on_unavailable``.
DISABLE_AND_REPORT = "disable_and_report"
FAIL = "fail"


class FeatureState(StrEnum):
    """Outcome of evaluating one build feature against the evidence."""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    #: No capabilities file for this connection, so nothing can be asserted.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Feature:
    """A build feature and the probes that must pass for it to be usable.

    Args:
        requires_all: Every probe id must be available.
        requires_any: At least one probe id must be available. Used where the
            design needs *a* model rather than a specific one, so a roster change
            does not disable the feature.
        fallback: What the build does instead when this is disabled. Recorded so
            the report states a consequence rather than only a loss.
    """

    description: str
    requires_all: tuple[str, ...] = ()
    requires_any: tuple[str, ...] = ()
    fallback: str = ""


#: Any confirmed generative path satisfies the narrative features. This spans
#: BOTH the SQL functions and the REST inference endpoints, because they are
#: separately gated: an account can serve one and refuse the other. Listed newest
#: first for readability; :meth:`Capabilities.evaluate` only asks whether one is
#: available.
_ANY_GENERATIVE_MODEL = (
    "AI_COMPLETE:claude-opus-5",
    "AI_COMPLETE:claude-sonnet-5",
    "AI_COMPLETE:claude-sonnet-4-6",
    "AI_COMPLETE:claude-haiku-4-5",
    "AI_COMPLETE:openai-gpt-5.1",
    "AI_COMPLETE:llama3.3-70b",
    "AI_COMPLETE:llama3.1-70b",
    "AI_COMPLETE:llama3.1-8b",
    "AI_COMPLETE:mistral-large2",
    "AI_COMPLETE:mistral-7b",
    "SNOWFLAKE.CORTEX.COMPLETE",
    # REST inference surfaces -- independent of the SQL path above.
    "CORTEX_REST_CHAT_COMPLETIONS",
    "CORTEX_REST_MESSAGES",
    "CORTEX_INFERENCE_COMPLETE_REST",
    # Pre-AI_* namespace, gated separately in principle.
    "LEGACY_SUMMARIZE",
)

#: The build's features, keyed as they are referred to in the architecture.
FEATURES: dict[str, Feature] = {
    # ---------------------------------------------------------- critical path #
    "dynamic_table_pipeline": Feature(
        description="All 8 dynamic tables (diagram 4)",
        requires_all=("CREATE_DYNAMIC_TABLE", "DYNAMIC_TABLE_REFRESHES"),
        fallback="None. The pipeline has no alternative implementation.",
    ),
    "signal_routing": Feature(
        description="SIGNALS_STREAM -> ROUTE_SIGNALS -> CASE_RECORD, idempotently",
        requires_all=(
            "STREAM_ON_DYNAMIC_TABLE",
            "CREATE_TASK_WAREHOUSE",
            "EXECUTE_TASK",
            "MERGE_WITH_UNIQUE_CONSTRAINT",
        ),
        fallback="None. Gap 11's idempotency mechanism depends on all four.",
    ),
    "agent_tool_procedures": Feature(
        description="The 8 agent tool procedures",
        requires_any=("PYTHON_STORED_PROCEDURE", "SQL_STORED_PROCEDURE"),
        fallback="SQL procedures only; any tool needing Python libraries is cut.",
    ),
    "mule_ring_traversal": Feature(
        description="Depth-3 counterparty ring detection",
        requires_all=("RECURSIVE_CTE",),
        fallback="Fixed two-hop joins; depth-3 rings go undetected.",
    ),
    "streamlit_app": Feature(
        description="The demo surface",
        requires_all=("STREAMLIT",),
        fallback="CLI output only.",
    ),
    # ------------------------------------------------------------------- ml #
    "fuzzy_sanctions": Feature(
        description="Fuzzy sanctions screening (ml.yml tier1.fuzzy_sanctions)",
        requires_all=("JAROWINKLER_SIMILARITY", "SOUNDEX"),
        fallback="Exact-match joins only; aliases and transliterations are missed.",
    ),
    "velocity_anomaly_ml": Feature(
        description="Per-account behavioural baseline (ml.yml tier1.velocity_anomaly)",
        requires_all=("SNOWFLAKE.ML.ANOMALY_DETECTION",),
        fallback="Fixed thresholds from POLICY_CLAUSE only.",
    ),
    "triage_classifier": Feature(
        description="Supervised queue ranking (ml.yml future_scope)",
        requires_all=("SNOWFLAKE.ML.CLASSIFICATION",),
        fallback="Rule severity ordering.",
    ),
    "ml_model_provenance": Feature(
        description="AUDIT.AGENT_ACTION_LOG.model_version as a registered artefact",
        requires_all=("ML_MODEL_REGISTRY",),
        fallback="model_version recorded as a free-text string, not a registry reference.",
    ),
    "feature_store": Feature(
        description="Point-in-time-correct training sets (ml.yml future_scope)",
        requires_all=("ML_FEATURE_STORE",),
        fallback="No supervised training, so no leakage risk to mitigate.",
    ),
    # ----------------------------------------------------------- governance #
    "row_level_security": Feature(
        description="'Same question, two roles, two answers' (diagram 11, G1)",
        requires_all=("ROW_ACCESS_POLICY",),
        fallback="Parameterised views prove equivalent filtering; not a policy object.",
    ),
    "pii_masking": Feature(
        description="PAN / Aadhaar masking for non-privileged roles",
        requires_all=("MASKING_POLICY",),
        fallback="Masked at generation time; no role-dependent unmasking to demonstrate.",
    ),
    "data_quality_gates": Feature(
        description="DMF pipeline-health gates (diagram 11 G6, Suite 5)",
        requires_all=("CREATE_DMF", "ASSOCIATE_DMF"),
        fallback="SQL assertions written to AUDIT.DATA_QUALITY_LOG.",
    ),
    "time_travel_reverification": Feature(
        description="Reviewer recompute against historical table state",
        requires_all=("RETENTION_14_DAYS",),
        fallback="EVIDENCE_QUERY_LOG.result_snapshot, which is the real mechanism (Gap 15).",
    ),
    # --------------------------------------------------------------- cortex #
    "semantic_layer": Feature(
        description="Semantic view object and SQL-level querying",
        requires_all=("CREATE_SEMANTIC_VIEW", "QUERY_SEMANTIC_VIEW"),
        fallback="Plain views; no governed metric definitions.",
    ),
    "analyst_nl_query": Feature(
        description="Natural-language querying of the semantic view (Suite 2)",
        requires_all=("CORTEX_ANALYST_REST",),
        fallback="Golden questions authored now, scored when Cortex is available.",
    ),
    "agent_layer": Feature(
        description="Cortex Agent object and invocation (diagrams 7, 13)",
        requires_all=("CREATE_AGENT",),
        # Four documented invocation paths. Any one of them is enough, so the
        # feature is only disabled once all four are proven blocked -- the
        # mistake that made Cortex Analyst look unavailable was testing one.
        requires_any=(
            "DATA_AGENT_RUN",
            "AGENT_RUN_SQL_NO_OBJECT",
            "AGENT_RUN_REST_OBJECT",
            "AGENT_RUN_REST_OBJECT_STREAMING",
            "AGENT_RUN_REST_INLINE",
        ),
        fallback="Mode interface with recorded transcript fixtures; Analyst serves NLQ directly.",
    ),
    "agent_narrative_modes": Feature(
        description="Drafter and Reviewer narrative generation",
        requires_any=_ANY_GENERATIVE_MODEL,
        fallback="Deterministic Reviewer checks run for real; narrative is templated.",
    ),
    "bulk_summarisation": Feature(
        description="Cross-row narrative summarisation for evidence bundles",
        # The one generative capability that IS served on a trial account. Worth a
        # feature of its own so the build can use it instead of assuming all
        # generation is lost.
        requires_any=("AI_AGG", "AI_SUMMARIZE_AGG"),
        fallback="Concatenate evidence rows without summarisation.",
    ),
    "l3_narratives": Feature(
        description="10K KYC notes and 2K case comments (diagram 2, L3)",
        requires_any=_ANY_GENERATIVE_MODEL,
        fallback="Template-based generator with the same noise/contradiction rates.",
    ),
    "regulatory_search": Feature(
        description="REGULATORY_SEARCH_SVC clause retrieval and citation grounding",
        # Both the default embedding model and an explicitly named one, since
        # naming one could plausibly have avoided the default's gate.
        requires_any=("CORTEX_SEARCH_SERVICE", "CORTEX_SEARCH_EXPLICIT_EMBEDDING_MODEL"),
        fallback="ILIKE / JAROWINKLER_SIMILARITY over POLICY_CLAUSE_CHUNKS.",
    ),
    "policy_ingestion": Feature(
        description="Clause extraction from the FIU circular (diagrams 3, 10)",
        requires_all=("AI_PARSE_DOCUMENT",),
        # EXTRACT_ANSWER is the pre-AI_* extractive-QA function and would serve as
        # a substitute for AI_EXTRACT if it were available.
        requires_any=("AI_EXTRACT", "LEGACY_EXTRACT_ANSWER"),
        fallback="POLICY_CLAUSE seeded from conf/detection.yml with hand-authored provenance.",
    ),
    "groundedness_eval": Feature(
        description="LLM-as-judge groundedness metrics (Suite 3)",
        # The function resolving is necessary but not sufficient: the judge is an
        # LLM, so a model must also be reachable. Requiring only the entry point
        # would report this as available on an account that cannot run a judge.
        requires_all=("EXECUTE_AI_EVALUATION",),
        requires_any=_ANY_GENERATIVE_MODEL,
        fallback="Deterministic citation-resolution checks only.",
    ),
    "contradiction_detection": Feature(
        description="KYC narratives that contradict the structured record",
        requires_any=("AI_CLASSIFY", "AI_FILTER", "AI_SENTIMENT", "LEGACY_SENTIMENT"),
        fallback="Both facts surfaced; the agent reconciles them.",
    ),
    "embedding_entity_resolution": Feature(
        description="Alias/transliteration resolution via embeddings",
        # Includes the underlying EMBED_TEXT_* functions and translation, any of
        # which would give a transliteration-tolerant path.
        requires_any=(
            "AI_EMBED",
            "AI_SIMILARITY",
            "EMBED_TEXT_768",
            "EMBED_TEXT_1024",
            "AI_TRANSLATE",
            "LEGACY_TRANSLATE",
        ),
        fallback="Jaro-Winkler, which fails on transliteration variants.",
    ),
    # ------------------------------------------------------------- external #
    "notifications": Feature(
        description="Case routing and notification dispatch (diagram 9)",
        requires_all=("NOTIFICATION_INTEGRATION_EMAIL", "SEND_EMAIL"),
        fallback="Cases created without notification.",
    ),
    "external_loop": Feature(
        description="Jira / Slack / MCP loop closing (diagram 13)",
        requires_all=("NETWORK_RULE", "EXTERNAL_ACCESS_INTEGRATION"),
        fallback="Loop closing demonstrated against recorded fixtures.",
    ),
}


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    """Evaluation of one feature, with the evidence that decided it."""

    name: str
    state: FeatureState
    description: str
    fallback: str
    #: Probe ids that are unavailable, each with its failure class and code.
    blockers: tuple[BlockerDetail, ...] = ()
    #: Probe ids the capabilities file says nothing about.
    untested: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.state is FeatureState.ENABLED

    def summary(self) -> str:
        """One line explaining the state, naming causes rather than implying them."""
        if self.state is FeatureState.ENABLED:
            return "available"
        if self.state is FeatureState.UNKNOWN:
            missing = ", ".join(self.untested[:4]) or "no capability evidence"
            return f"not established ({missing})"
        causes = "; ".join(
            f"{b.probe_id} [{b.failure_class or 'unclassified'}"
            + (f" {b.error_code}" if b.error_code else "")
            + "]"
            for b in self.blockers[:4]
        )
        return f"blocked by {causes}" if causes else "blocked"


@dataclass(frozen=True, slots=True)
class BlockerDetail:
    probe_id: str
    failure_class: str | None
    error_code: str | None


@dataclass(slots=True)
class Capabilities:
    """The probe results for one connection."""

    connection: str
    probed_at: str
    account: dict[str, Any] = field(default_factory=dict)
    probes: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: Path | None = None

    @property
    def edition(self) -> str | None:
        value = self.account.get("edition")
        return str(value) if value else None

    def available(self, probe_id: str) -> bool | None:
        """Return availability, or ``None`` when the probe was never run.

        The tri-state matters: ``None`` means no evidence, and this module
        deliberately refuses to read that as either yes or no.
        """
        entry = self.probes.get(probe_id)
        if entry is None:
            return None
        return bool(entry.get("available"))

    def _blocker(self, probe_id: str) -> BlockerDetail:
        entry = self.probes.get(probe_id) or {}
        code = entry.get("error_code")
        return BlockerDetail(
            probe_id=probe_id,
            failure_class=entry.get("failure_class"),
            error_code=str(code) if code is not None else None,
        )

    def evaluate(self, name: str, feature: Feature) -> FeatureStatus:
        """Decide whether ``feature`` can be built, given the evidence."""
        blockers: list[BlockerDetail] = []
        untested: list[str] = []

        for probe_id in feature.requires_all:
            outcome = self.available(probe_id)
            if outcome is None:
                untested.append(probe_id)
            elif not outcome:
                blockers.append(self._blocker(probe_id))

        if feature.requires_any:
            outcomes = {pid: self.available(pid) for pid in feature.requires_any}
            if not any(outcomes.values()):
                if all(v is None for v in outcomes.values()):
                    untested.extend(feature.requires_any)
                else:
                    blockers.extend(self._blocker(pid) for pid, v in outcomes.items() if v is False)

        if blockers:
            state = FeatureState.DISABLED
        elif untested:
            # Untested is not available. Building on it is exactly the assumption
            # this module exists to stop.
            state = FeatureState.UNKNOWN
        else:
            state = FeatureState.ENABLED

        return FeatureStatus(
            name=name,
            state=state,
            description=feature.description,
            fallback=feature.fallback,
            blockers=tuple(blockers),
            untested=tuple(dict.fromkeys(untested)),
        )

    def evaluate_all(self) -> dict[str, FeatureStatus]:
        """Evaluate every feature in :data:`FEATURES`, in declaration order."""
        return {name: self.evaluate(name, spec) for name, spec in FEATURES.items()}


def capabilities_path(settings: Settings) -> Path:
    """Where the probe writes evidence for the active connection.

    Delegates to :func:`risk_copilot.config.capabilities_file`, which owns the
    path because ``config`` also reads this file to clamp Time Travel retention
    and the two must never disagree about where the evidence lives.
    """
    return capabilities_file(settings.repo_root, settings.connection_name)


def load_capabilities(settings: Settings) -> Capabilities | None:
    """Read the capabilities file for the active connection, or ``None``.

    A missing file is not an error: the probe is a separate, deliberate step, and
    offline work does not need it. The caller reports the absence.
    """
    path = capabilities_path(settings)
    if not path.is_file():
        return None

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.debug("Could not read %s", path, exc_info=True)
        return None

    if not isinstance(payload, dict):
        return None

    probes = payload.get("probes")
    account = payload.get("account")
    return Capabilities(
        connection=str(payload.get("connection", settings.connection_name)),
        probed_at=str(payload.get("probed_at", "unknown")),
        account=account if isinstance(account, dict) else {},
        probes=probes if isinstance(probes, dict) else {},
        source=path,
    )


def on_unavailable_policy(settings: Settings) -> str:
    """Return the configured policy for an unmet precondition.

    Defaults to ``disable_and_report``: a hard failure at demo time is the worst
    available outcome, so it is never the default.
    """
    preconditions = (settings.ml.get("preconditions") or {}) if settings.ml else {}
    value = str(preconditions.get("on_unavailable") or DISABLE_AND_REPORT).strip().lower()
    return value if value in (DISABLE_AND_REPORT, FAIL) else DISABLE_AND_REPORT
