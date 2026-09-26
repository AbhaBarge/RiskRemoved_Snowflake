-- ===========================================================================
-- 01_ddl / 03_evidence_chain.sql
-- ===========================================================================
-- SIGNAL -> CASE -> EVIDENCE -> FINDING -> REVIEW -> ARTIFACT -> ATTESTATION.
--
-- Append-only by discipline: FINDING and ARTIFACT are versioned rather than
-- updated, because an overwritten narrative destroys the record of what was
-- concluded and when.
--
-- Two gaps are resolved structurally here:
--
-- GAP-11  Idempotency. A dynamic table recomputes its whole result set every
--         refresh, so the same logical signal reappears every five minutes.
--         signal_business_key gives the router something to MERGE on; without
--         it, the stream-plus-task design inserts duplicate cases forever.
--
-- GAP-12  Re-verification. The Reviewer cannot rely on Time Travel alone:
--         AT(TIMESTAMP=>) binds per table reference rather than per statement,
--         and dynamic tables are not freely time-travellable. EVIDENCE_QUERY_LOG
--         therefore stores a canonical result SNAPSHOT plus its hash. Recompute
--         and compare is the correctness mechanism; Time Travel is the
--         demonstrable provenance layer on top of it.
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

-- --------------------------------------------------------------------------
-- Signals
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS SIGNAL (
    signal_id           VARCHAR NOT NULL,
    signal_business_key VARCHAR NOT NULL COMMENT 'rule_id|party_id|window_bucket[|account_id] (GAP-11). The MERGE key that makes routing idempotent.',
    rule_id             VARCHAR NOT NULL,
    rule_version        NUMBER(10,0) NOT NULL COMMENT 'Pins which rule version fired, so a later threshold change cannot retroactively alter a past finding.',
    party_id            VARCHAR NOT NULL,
    account_id          VARCHAR COMMENT 'NULL for party-grain rules.',
    severity            VARCHAR NOT NULL,
    window_bucket       VARCHAR NOT NULL COMMENT 'Canonical observation window, e.g. 2022-09 or 2022-09-14.',
    fired_at            TIMESTAMP_NTZ NOT NULL,
    as_of_ts            TIMESTAMP_NTZ NOT NULL COMMENT 'Data snapshot the rule evaluated. Time Travel target.',
    metric_value        NUMBER(20,4) COMMENT 'The observed value, from SQL. Never from a model.',
    threshold_value     NUMBER(20,4) COMMENT 'The clause threshold it breached, resolved from POLICY_CLAUSE.',
    evidence_txn_ids    ARRAY   COMMENT 'Transactions that caused the firing. The audit trail of the detection.',
    detail              VARIANT COMMENT 'Rule-specific context, e.g. the shared fingerprint, the remitter count.',
    first_seen_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    last_seen_at        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL
                                COMMENT 'Bumped when the same business key re-fires, instead of inserting a duplicate.',
    CONSTRAINT pk_signal PRIMARY KEY (signal_id),
    CONSTRAINT uk_signal_business_key UNIQUE (signal_business_key)
)
COMMENT = 'Persisted rule firings, deduplicated on signal_business_key.';

-- --------------------------------------------------------------------------
-- Cases
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CASE_RECORD (
    case_id             VARCHAR NOT NULL,
    party_id            VARCHAR NOT NULL,
    case_type           VARCHAR NOT NULL COMMENT 'FRAUD | AML | CROSS_DOMAIN. Two or more distinct rule families on one party escalate to CROSS_DOMAIN.',
    status              VARCHAR NOT NULL DEFAULT 'OPEN'
                                COMMENT 'OPEN | INVESTIGATING | DRAFTED | REVIEWED | CLOSED.',
    priority            NUMBER(5,0) COMMENT 'Sentinel triage rank. Advisory; never a filing input.',
    assigned_to         VARCHAR,
    branch_region       VARCHAR NOT NULL COMMENT 'Denormalised so the row access policy can filter cases too. Without it a branch analyst could read an out-of-region case and the governance claim would be false.',
    opened_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    suspicion_established_at TIMESTAMP_NTZ COMMENT 'Starts the statutory clock. Distinct from opened_at, because the 7-working-day deadline runs from suspicion, not from alert.',
    sla_deadline        TIMESTAMP_NTZ COMMENT 'Computed from STR_FILING_SLA_DAYS in POLICY_CLAUSE.',
    closed_at           TIMESTAMP_NTZ,
    closure_reason      VARCHAR,
    updated_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_case PRIMARY KEY (case_id)
)
COMMENT = 'Investigation cases grouping related signals. Named CASE_RECORD because CASE is reserved.';

CREATE TABLE IF NOT EXISTS CASE_SIGNAL (
    case_id     VARCHAR NOT NULL,
    signal_id   VARCHAR NOT NULL,
    attached_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_case_signal PRIMARY KEY (case_id, signal_id),
    CONSTRAINT fk_case_signal_case FOREIGN KEY (case_id) REFERENCES CASE_RECORD (case_id),
    CONSTRAINT fk_case_signal_signal FOREIGN KEY (signal_id) REFERENCES SIGNAL (signal_id)
)
COMMENT = 'Many-to-many signal-to-case link. A signal can be relevant to more than one case.';

-- --------------------------------------------------------------------------
-- Evidence -- pointers, never copies.
--
-- Copying source rows would make evidence diverge from the record it claims to
-- describe. A pointer plus as_of_ts stays true.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS EVIDENCE (
    evidence_id     VARCHAR NOT NULL,
    case_id         VARCHAR NOT NULL,
    ref_type        VARCHAR NOT NULL COMMENT 'TRANSACTION | PARTY | ACCOUNT | SCREENING | POLICY_CLAUSE | RELATIONSHIP | KYC_NOTE.',
    ref_id          VARCHAR NOT NULL COMMENT 'Primary key in the referenced table.',
    ref_table       VARCHAR NOT NULL COMMENT 'Fully qualified source table. Makes the pointer resolvable without a hardcoded lookup in the Reviewer.',
    as_of_ts        TIMESTAMP_NTZ NOT NULL COMMENT 'Snapshot the evidence was gathered at.',
    materiality     VARCHAR COMMENT 'PRIMARY | SUPPORTING | CONTEXT. The Reviewer rejects an artifact whose only PRIMARY evidence comes from an inadmissible source.',
    gathered_by     VARCHAR COMMENT 'Agent session or procedure that gathered it.',
    created_at      TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_evidence PRIMARY KEY (evidence_id),
    CONSTRAINT fk_evidence_case FOREIGN KEY (case_id) REFERENCES CASE_RECORD (case_id)
)
COMMENT = 'Immutable pointers to source rows. Never copies of them.';

-- --------------------------------------------------------------------------
-- GAP-12: the reproducibility log.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS EVIDENCE_QUERY_LOG (
    query_log_id        VARCHAR NOT NULL,
    evidence_id         VARCHAR NOT NULL,
    sql_text            VARCHAR NOT NULL COMMENT 'The exact statement executed.',
    sql_hash            VARCHAR NOT NULL COMMENT 'SHA-256 of sql_text, so a rewritten query is detectable.',
    bind_values         VARIANT COMMENT 'Parameters, kept separate: the SQL is never string-interpolated.',
    result_columns      ARRAY   NOT NULL COMMENT 'Column names in query order. Part of the canonical form.',
    result_row_count    NUMBER(38,0) NOT NULL,
    result_snapshot     VARIANT COMMENT 'Canonical result set. THE re-verification baseline (GAP-12): Time Travel cannot be relied on, since AT(TIMESTAMP=>) binds per table reference and dynamic tables are not freely time-travellable.',
    result_hash         VARCHAR NOT NULL COMMENT 'SHA-256 over the canonical form. See risk_copilot.hashing.',
    canonical_form_version VARCHAR NOT NULL DEFAULT '1'
                                COMMENT 'Guards against comparing an old hash with a new algorithm, which would look like tampered evidence.',
    snowflake_query_id  VARCHAR COMMENT 'For ACCOUNT_USAGE correlation. NOT used for re-verification: RESULT_SCAN is session-scoped and expires after 24 hours.',
    warehouse_name      VARCHAR,
    executed_at         TIMESTAMP_NTZ NOT NULL,
    execution_ms        NUMBER(38,0),
    CONSTRAINT pk_evidence_query_log PRIMARY KEY (query_log_id),
    CONSTRAINT fk_evidence_query_log_evidence FOREIGN KEY (evidence_id) REFERENCES EVIDENCE (evidence_id)
)
COMMENT = 'Query provenance per evidence row. Recompute-and-compare is the correctness mechanism.';

-- --------------------------------------------------------------------------
-- Findings -- versioned, never updated.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS FINDING (
    finding_id          VARCHAR NOT NULL,
    case_id             VARCHAR NOT NULL,
    version             NUMBER(10,0) NOT NULL DEFAULT 1 COMMENT 'A rejected draft is superseded, not overwritten: the Reviewer''s catch has to remain visible.',
    narrative           VARCHAR NOT NULL COMMENT 'LLM-composed prose. The ONLY model-authored field that reaches a filing, and it embeds no computed figure.',
    conclusion          VARCHAR NOT NULL COMMENT 'SUSPICIOUS | CLEARED | ESCALATE.',
    cited_clause_ids    ARRAY   NOT NULL COMMENT 'Must resolve to POLICY_CLAUSE rows in force on the transaction date.',
    cited_evidence_ids  ARRAY   NOT NULL COMMENT 'Must resolve to EVIDENCE rows. Checked deterministically, with a 100% target -- not judged by a model.',
    computed_values     VARIANT COMMENT 'Every numeral the narrative refers to, keyed by placeholder, each carrying the query_log_id it came from. This is the mechanism behind "no number in a filing comes from a language model".',
    author_model        VARCHAR NOT NULL COMMENT 'Resolved model that drafted this version.',
    prompt_version      VARCHAR NOT NULL,
    agent_session_id    VARCHAR COMMENT 'Joins to AUDIT.AGENT_ACTION_LOG.',
    supersedes_finding_id VARCHAR,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_finding PRIMARY KEY (finding_id),
    CONSTRAINT fk_finding_case FOREIGN KEY (case_id) REFERENCES CASE_RECORD (case_id)
)
COMMENT = 'Versioned conclusions. computed_values ties every figure to the query that produced it.';

-- --------------------------------------------------------------------------
-- Adversarial review.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS REVIEW (
    review_id       VARCHAR NOT NULL,
    finding_id      VARCHAR NOT NULL,
    reviewer_type   VARCHAR NOT NULL COMMENT 'ADVERSARIAL_AGENT | HUMAN.',
    decision        VARCHAR NOT NULL COMMENT 'APPROVE | REJECT.',
    defects_found   VARIANT COMMENT 'Specific, actionable defects. A bare REJECT is useless to the Drafter, so the schema expects the list.',
    checks_run      VARIANT COMMENT 'Which validations executed and their outcomes, so an APPROVE is auditable rather than asserted.',
    numeric_recompute_passed BOOLEAN COMMENT 'Did every figure in computed_values match a recomputed result?',
    citation_resolution_passed BOOLEAN COMMENT 'Did every cited id resolve? Target: 100%.',
    clause_in_effect_passed  BOOLEAN COMMENT 'Was every cited clause in force on the transaction date?',
    reviewer_model  VARCHAR COMMENT 'Deliberately a different family from author_model: a critic sharing the drafter''s blind spots is not a critic.',
    reviewed_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_review PRIMARY KEY (review_id),
    CONSTRAINT fk_review_finding FOREIGN KEY (finding_id) REFERENCES FINDING (finding_id)
)
COMMENT = 'Adversarial validation results. Boolean columns are deterministic checks, not model opinions.';

-- --------------------------------------------------------------------------
-- Artifacts
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ARTIFACT (
    artifact_id         VARCHAR NOT NULL,
    finding_id          VARCHAR NOT NULL,
    artifact_type       VARCHAR NOT NULL COMMENT 'STR | CTR | PROVISION_REPORT.',
    template_id         VARCHAR COMMENT 'FK to GOVERNANCE.REF_FILING_TEMPLATES.',
    template_version    VARCHAR,
    version             NUMBER(10,0) NOT NULL DEFAULT 1,
    content             VARIANT NOT NULL COMMENT 'Structured filing payload, assembled by Python from SQL results. The model contributes only the narrative text.',
    content_rendered    VARCHAR COMMENT 'Serialised XML/JSON as it would be submitted.',
    content_hash        VARCHAR NOT NULL COMMENT 'SHA-256 of content. Binds an attestation to one exact version: if content changes, the attestation no longer matches.',
    status              VARCHAR NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT | REVIEWED | APPROVED | FILED.',
    validation_status   VARCHAR COMMENT 'PASS | FAIL against the template schema.',
    validation_defects  VARIANT,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_artifact PRIMARY KEY (artifact_id),
    CONSTRAINT fk_artifact_finding FOREIGN KEY (finding_id) REFERENCES FINDING (finding_id)
)
COMMENT = 'Filing artifacts. Numbers assembled deterministically; narrative embedded from FINDING.';

-- --------------------------------------------------------------------------
-- Attestation -- the human gate.
--
-- Under PML Rules Rule 7 the Principal Officer bears the filing obligation.
-- Autonomous filing is legally invalid, not merely imprudent, so no status can
-- reach FILED without a row here. The rule is enforced by a privilege (only the
-- compliance role may INSERT) and by a trigger-equivalent check in the filing
-- procedure -- never by an instruction in a prompt.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ATTESTATION (
    attestation_id      VARCHAR NOT NULL,
    artifact_id         VARCHAR NOT NULL,
    attested_content_hash VARCHAR NOT NULL COMMENT 'The hash the officer actually signed. Compared against the artifact at filing time, so a post-attestation edit is caught.',
    attestor            VARCHAR NOT NULL,
    attestor_role       VARCHAR NOT NULL COMMENT 'PRINCIPAL_OFFICER | DESIGNATED_DIRECTOR.',
    decision            VARCHAR NOT NULL COMMENT 'ATTESTED | RETURNED.',
    comment             VARCHAR,
    attested_at         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_attestation PRIMARY KEY (attestation_id),
    CONSTRAINT fk_attestation_artifact FOREIGN KEY (artifact_id) REFERENCES ARTIFACT (artifact_id)
)
COMMENT = 'Human sign-off. No artifact reaches FILED without a matching, hash-bound attestation.';
