-- ===========================================================================
-- 01_ddl / 02_governance.sql
-- ===========================================================================
-- Human-gated state: the regulatory thresholds, the typology catalogue, the
-- rule registry and the filing templates.
--
-- POLICY_CLAUSE is the runtime source of truth for every threshold. Detection
-- SQL joins to it rather than embedding a literal, which is what turns
-- "policy-as-data" from a slide into a property of the system: changing the CTR
-- threshold is a governed, effective-dated data change with an audit trail,
-- not a code deploy.
-- ===========================================================================

USE SCHEMA {{ fq.governance }};

-- --------------------------------------------------------------------------
-- Effective-dated regulatory and internal thresholds.
--
-- Seeded from conf/detection.yml, which carries the provenance that the deploy
-- gate requires: a clause with no source_document is rejected, which is how the
-- demo is prevented from citing a circular number that does not exist.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS POLICY_CLAUSE (
    clause_id               VARCHAR NOT NULL,
    clause_version          NUMBER(10,0) NOT NULL DEFAULT 1,
    source_document         VARCHAR NOT NULL COMMENT 'Real regulation or named internal document. Never blank.',
    source_page             NUMBER(10,0),
    section_heading         VARCHAR,
    clause_text             VARCHAR COMMENT 'Quoted or paraphrased text, cited verbatim in findings.',
    metric                  VARCHAR NOT NULL COMMENT 'What is measured, e.g. cash_txn_amount_inr.',
    operator                VARCHAR NOT NULL COMMENT '>= | <= | = | BETWEEN.',
    threshold_lower         NUMBER(20,4),
    threshold_upper         NUMBER(20,4) COMMENT 'Populated only for BETWEEN.',
    action_required         VARCHAR COMMENT 'FILE_CTR | FILE_STR | RAISE_SIGNAL | MANUAL_REVIEW | BLOCK_AND_ESCALATE | BLOCK_AUTO_FILING.',
    effective_from          DATE    NOT NULL,
    effective_to            DATE    COMMENT 'NULL = currently in force. Effective-dating lets a finding cite the clause that was in force on the transaction date, not today''s.',
    supersedes_clause_id    VARCHAR,
    approved_by             VARCHAR COMMENT 'Who promoted this clause. NULL only for the config-seeded baseline.',
    approved_at             TIMESTAMP_NTZ,
    created_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_policy_clause PRIMARY KEY (clause_id, clause_version)
)
COMMENT = 'Effective-dated thresholds. THE runtime source of truth: detection SQL joins here instead of hardcoding a number.';

-- --------------------------------------------------------------------------
-- Current-clause convenience view. Detection SQL reads this so that no query
-- has to remember the effective-dating predicate -- forgetting it would
-- silently apply a superseded threshold.
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW V_POLICY_CLAUSE_CURRENT
    COMMENT = 'Clauses in force today, latest version per clause_id.'
AS
SELECT *
FROM POLICY_CLAUSE
QUALIFY ROW_NUMBER() OVER (PARTITION BY clause_id ORDER BY clause_version DESC) = 1
    AND effective_from <= CURRENT_DATE()
    AND (effective_to IS NULL OR effective_to > CURRENT_DATE());

-- --------------------------------------------------------------------------
-- Typology catalogue, anchored to real FATF / RBI references so a finding can
-- name the laundering pattern it alleges rather than gesturing at one.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TYPOLOGY (
    typology_id         VARCHAR NOT NULL,
    name                VARCHAR NOT NULL COMMENT 'structuring | layering | mule_fan_in | cycle | ...',
    fatf_reference      VARCHAR,
    rbi_reference       VARCHAR,
    description         VARCHAR,
    detection_approach  VARCHAR COMMENT 'rules | graph | hybrid | ml.',
    -- AMLworld labels patterns as CYCLE, FAN-OUT, GATHER-SCATTER and so on.
    -- Storing that mapping here is what lets per-typology F1 be computed
    -- without a hardcoded translation table in the eval script.
    amlworld_pattern    VARCHAR COMMENT 'Corresponding HI-Small_Patterns.txt label, if any.',
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_typology PRIMARY KEY (typology_id)
)
COMMENT = 'Laundering typologies with regulatory citations and their AMLworld pattern names.';

-- --------------------------------------------------------------------------
-- Rule registry. Versioned: a signal records which rule version fired, so a
-- later threshold change cannot retroactively alter what a past finding claimed.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RULE_DEFINITION (
    rule_id             VARCHAR NOT NULL,
    rule_version        NUMBER(10,0) NOT NULL DEFAULT 1,
    typology_id         VARCHAR COMMENT 'FK to TYPOLOGY.',
    clause_ids          ARRAY   COMMENT 'Clauses this rule implements. A rule with no clause is an ungoverned rule and is rejected at deploy time.',
    description         VARCHAR NOT NULL,
    detection_grain     VARCHAR NOT NULL COMMENT 'TRANSACTION | ACCOUNT_WINDOW | PARTY_WINDOW. Needed because evaluation must compare like with like: labels are at transaction grain, signals are not.',
    window_bucket_expr  VARCHAR COMMENT 'SQL expression producing the dedup window bucket (GAP-11).',
    rule_sql_hash       VARCHAR COMMENT 'SHA-256 of the detection SQL, so a silent logic change is visible.',
    severity            VARCHAR NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- An ML-derived signal may rank work but may never be the sole basis of a
    -- filing. Enforced by validate_filing_schema(), not left to a prompt.
    admissible_as_primary_evidence BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_rule_definition PRIMARY KEY (rule_id, rule_version),
    CONSTRAINT fk_rule_typology FOREIGN KEY (typology_id) REFERENCES TYPOLOGY (typology_id)
)
COMMENT = 'Versioned detection rules, each traceable to the policy clauses it implements.';

-- --------------------------------------------------------------------------
-- Policy change proposals -- the human gate (Diagram 10).
--
-- Extraction lands in RAW_POLICY_CLAUSES; a diff lands here; a compliance
-- officer approves or rejects. Nothing reaches POLICY_CLAUSE unreviewed, which
-- is the difference between a copilot and an unsupervised rule-writer.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS POLICY_CHANGE_PROPOSALS (
    proposal_id         VARCHAR NOT NULL,
    staging_id          VARCHAR COMMENT 'FK to RAW.RAW_POLICY_CLAUSES.',
    change_type         VARCHAR NOT NULL COMMENT 'NEW | CHANGED | SUPERSEDED.',
    target_clause_id    VARCHAR COMMENT 'NULL for NEW.',
    proposed_payload    VARIANT NOT NULL COMMENT 'The clause row that would be written.',
    current_payload     VARIANT COMMENT 'What it would replace, for a reviewable diff.',
    diff_summary        VARCHAR COMMENT 'Human-readable summary of what changes.',
    status              VARCHAR NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING | APPROVED | REJECTED.',
    reviewed_by         VARCHAR,
    reviewed_at         TIMESTAMP_NTZ,
    review_comment      VARCHAR COMMENT 'Required on rejection: a rejection without a reason is not a control.',
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_policy_change_proposals PRIMARY KEY (proposal_id)
)
COMMENT = 'Human-gated queue between policy extraction and activation. No auto-activation.';

-- --------------------------------------------------------------------------
-- Clause chunks for Cortex Search (Gap 10).
--
-- Separate from POLICY_CLAUSE on purpose -- the two layers answer different
-- questions. This one is retrieval ("what does the rulebook say about X"),
-- POLICY_CLAUSE is execution ("what is the threshold"). Conflating them would
-- mean either unsearchable rules or unexecutable prose.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {{ cortex.search_source_table }} (
    chunk_id            VARCHAR NOT NULL,
    clause_id           VARCHAR COMMENT 'Set when the chunk maps to an executable clause.',
    source_document     VARCHAR NOT NULL,
    source_page         NUMBER(10,0),
    section_heading     VARCHAR,
    clause_text         VARCHAR NOT NULL COMMENT 'The search column.',
    effective_from      DATE,
    effective_to        DATE    COMMENT 'Filter attribute: current clauses only, unless asked otherwise.',
    supersedes_clause_id VARCHAR,
    document_type       VARCHAR COMMENT 'STATUTE | CIRCULAR | INTERNAL_POLICY | GUIDANCE.',
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_policy_clause_chunks PRIMARY KEY (chunk_id)
)
COMMENT = 'Clause-level chunks backing the Cortex Search service. Retrieval layer; POLICY_CLAUSE is the execution layer.';

-- --------------------------------------------------------------------------
-- Filing templates (GAP-17).
--
-- FIU-IND publishes a FORM, not a public XML schema. We therefore store our own
-- JSON-Schema transcription of the 8-Part SBA structure and validate against
-- that, stating plainly that this is our spec. Claiming FINGate conformance on
-- the basis of a transcribed PDF would be a false assertion in a compliance
-- demo, which is worse than an acknowledged gap.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS REF_FILING_TEMPLATES (
    template_id         VARCHAR NOT NULL,
    artifact_type       VARCHAR NOT NULL COMMENT 'STR | CTR | NTR | CBWTR.',
    template_version    VARCHAR NOT NULL,
    regulator           VARCHAR NOT NULL DEFAULT 'FIU-IND',
    source_form         VARCHAR COMMENT 'e.g. SBA01-SBA04 (banking company STR).',
    field_definitions   VARIANT NOT NULL COMMENT 'JSON-Schema for the 8 Parts: required fields, types, enumerations, which fields are deterministic vs narrative.',
    schema_authority    VARCHAR NOT NULL DEFAULT 'INTERNAL_TRANSCRIPTION'
                                COMMENT 'INTERNAL_TRANSCRIPTION | REGULATOR_PUBLISHED. Honest provenance of the schema itself.',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_ref_filing_templates PRIMARY KEY (template_id, template_version)
)
COMMENT = 'Filing structures. schema_authority records whether the schema is ours or the regulator''s.';

CREATE TABLE IF NOT EXISTS REF_FATF_JURISDICTIONS (
    jurisdiction_code   VARCHAR NOT NULL,
    jurisdiction_name   VARCHAR NOT NULL,
    fatf_status         VARCHAR NOT NULL COMMENT 'HIGH_RISK (black list) | INCREASED_MONITORING (grey) | COMPLIANT.',
    listed_date         DATE,
    delisted_date       DATE,
    statement_reference VARCHAR,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_ref_fatf PRIMARY KEY (jurisdiction_code)
)
COMMENT = 'FATF high-risk and increased-monitoring jurisdictions. Drives cross-border risk weighting.';

CREATE TABLE IF NOT EXISTS REF_CHANNEL (
    channel_code    VARCHAR NOT NULL,
    channel_name    VARCHAR NOT NULL,
    is_cash         BOOLEAN NOT NULL COMMENT 'Cash-equivalence drives CTR applicability. A wrong value here silently changes which transactions are reportable.',
    is_instant      BOOLEAN NOT NULL,
    typical_max_inr NUMBER(20,2) COMMENT 'Rail limit, e.g. IMPS caps well below RTGS.',
    CONSTRAINT pk_ref_channel PRIMARY KEY (channel_code)
)
COMMENT = 'Payment rail reference. is_cash determines CTR applicability.';
