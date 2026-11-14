-- ===========================================================================
-- 01_ddl / 04_audit_and_ground_truth.sql
-- ===========================================================================
-- Two schemas with opposite visibility rules:
--
--   AUDIT         written by the service role, read by compliance, never
--                 updated or deleted by anyone.
--   GROUND_TRUTH  granted to nobody but the admin role. The service role has no
--                 USAGE on the schema, so no prompt can reach a label.
-- ===========================================================================

USE SCHEMA {{ fq.audit }};

-- --------------------------------------------------------------------------
-- Every agent turn, in full.
--
-- The point is reconstructability: given a finding, this table must answer who
-- asked, under which role, which tools ran, what SQL executed, which model and
-- prompt produced the text, and what it cost. Anything less makes the
-- "inspectable thread" claim unverifiable.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS AGENT_ACTION_LOG (
    log_id              VARCHAR NOT NULL,
    session_id          VARCHAR NOT NULL COMMENT 'Groups the turns of one investigation.',
    turn_number         NUMBER(10,0) NOT NULL,
    agent_mode          VARCHAR NOT NULL COMMENT 'SENTINEL | INVESTIGATOR | DRAFTER | REVIEWER.',
    invoked_by          VARCHAR NOT NULL COMMENT 'USER | TASK | AUTOMATION | MCP_CLIENT.',
    snowflake_user      VARCHAR NOT NULL,
    user_role           VARCHAR NOT NULL COMMENT 'Effective role. Recorded because the row access policy makes the same question return different answers per role, and the answer is only interpretable alongside the role.',
    question_text       VARCHAR,
    tools_invoked       ARRAY   COMMENT 'Ordered tool calls for this turn.',
    tool_arguments      VARIANT COMMENT 'Arguments per call, so a tool invocation can be replayed.',
    sql_text            VARCHAR COMMENT 'Generated SQL, if any.',
    sql_hash            VARCHAR,
    snowflake_query_ids ARRAY   COMMENT 'For ACCOUNT_USAGE credit attribution.',
    rows_touched        NUMBER(38,0),
    evidence_ids_accessed ARRAY,
    case_ids_touched    ARRAY,
    model_version       VARCHAR NOT NULL COMMENT 'Resolved model, not the configured preference.',
    prompt_version      VARCHAR NOT NULL,
    tokens_input        NUMBER(38,0),
    tokens_output       NUMBER(38,0),
    credits_estimated   NUMBER(20,8),
    latency_ms          NUMBER(38,0),
    outcome             VARCHAR COMMENT 'SUCCESS | REFUSED | ERROR | PARTIAL.',
    refusal_reason      VARCHAR COMMENT 'Populated on REFUSED. A deliberate refusal is a feature, and the log is where it becomes demonstrable rather than anecdotal.',
    error_text          VARCHAR,
    logged_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_agent_action_log PRIMARY KEY (log_id)
)
COMMENT = 'Append-only audit trail of every agent turn. Never updated, never deleted.';

-- --------------------------------------------------------------------------
-- Data-quality observations (Diagram 11, gate 6).
--
-- DMF results are surfaced here so the agent can qualify an answer -- "this is
-- based on data 14 hours stale" -- instead of presenting stale output as
-- current. A confident answer over stale data is the more dangerous failure.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DATA_QUALITY_LOG (
    dq_log_id       VARCHAR NOT NULL,
    object_name     VARCHAR NOT NULL,
    metric_name     VARCHAR NOT NULL COMMENT 'NULL_RATE | ROW_COUNT | FRESHNESS_SECONDS | REFERENTIAL_ORPHANS | VOLUME_ANOMALY.',
    metric_value    NUMBER(20,4),
    threshold_value NUMBER(20,4),
    breached        BOOLEAN NOT NULL,
    measured_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_data_quality_log PRIMARY KEY (dq_log_id)
)
COMMENT = 'Data metric function results. Lets the agent qualify an answer by data freshness.';

-- --------------------------------------------------------------------------
-- One conversation across channels.
--
-- The dashboard's Ask page and the Slack bot each kept their own history (a
-- Streamlit session, a bot process's memory), so a question asked in Slack was
-- invisible on the dashboard and vice versa. Both now append every turn here,
-- and the Ask page reads the shared feed. Append-only, like the action log.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CHAT_MESSAGE (
    message_id      VARCHAR NOT NULL,
    channel         VARCHAR NOT NULL COMMENT 'APP | SLACK.',
    thread_ref      VARCHAR COMMENT 'Slack thread ts, or the app session id: groups follow-ups.',
    author          VARCHAR NOT NULL COMMENT 'Slack user id or app persona; never a password or token.',
    question        VARCHAR NOT NULL,
    answer_text     VARCHAR,
    answer_kind     VARCHAR COMMENT 'ANALYST | AGENT | STR | PDF | ERROR.',
    sql_text        VARCHAR COMMENT 'Cortex Analyst SQL, when the answer came from it.',
    row_count       NUMBER(38,0),
    case_id         VARCHAR,
    latency_ms      NUMBER(38,0),
    asked_at        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_chat_message PRIMARY KEY (message_id)
)
COMMENT = 'Ask-the-data turns from every channel (app and Slack), one shared feed. Append-only.';

-- ===========================================================================
-- GROUND_TRUTH -- isolated. No consumer role has USAGE on this schema.
-- ===========================================================================

USE SCHEMA {{ fq.ground_truth }};

-- --------------------------------------------------------------------------
-- GAP-08: leg-level labels.
--
-- AMLworld labels at PAIR grain, but the pipeline stores legs, so one labelled
-- source row yields two labelled legs. The Patterns file identifies illicit
-- transactions only by the tuple (timestamp, from_account, to_account, amount),
-- which is not unique -- the same tuple recurs. match_confidence records how the
-- label was attributed so an ambiguous match is visible in the metrics rather
-- than quietly inflating them.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS TXN_LABELS (
    txn_id              VARCHAR NOT NULL COMMENT 'Leg-level id. FK to RAW.RAW_TRANSACTIONS.',
    txn_pair_id         VARCHAR NOT NULL,
    src_row_number      NUMBER(38,0) COMMENT 'Source row. NULL for injected cohorts.',
    is_illicit          BOOLEAN NOT NULL,
    typology_id         VARCHAR COMMENT 'FK to GOVERNANCE.TYPOLOGY. NULL when AMLworld flagged the row but the Patterns file did not attribute a typology.',
    amlworld_pattern    VARCHAR COMMENT 'CYCLE | FAN-OUT | GATHER-SCATTER | ...',
    attempt_id          NUMBER(38,0) COMMENT 'Groups the legs of one laundering attempt, so recall can be measured per attempt rather than per leg.',
    injection_method    VARCHAR NOT NULL COMMENT 'amlworld_original | reskin_structuring | reskin_mule_fan_in | reskin_dormant_reactivation.',
    match_confidence    NUMBER(5,4) COMMENT 'Confidence of the Patterns-to-leg attribution. 1.0 = unambiguous. Below 1.0 means the source tuple was not unique.',
    inserted_at         TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_txn_labels PRIMARY KEY (txn_id)
)
COMMENT = 'Leg-level ground truth. NEVER exposed in the semantic view, and no consumer role can read it.';

-- --------------------------------------------------------------------------
-- GAP-08: the evaluation grain decision.
--
-- Labels are at transaction grain; signals fire at party-window or
-- account-window grain. "F1 > 0.7 for structuring" has no meaning until a
-- denominator is fixed. This table materialises the chosen grain -- party-month
-- -- so the metric is defined by an artefact rather than by whatever the
-- evaluation script happened to do.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS EVAL_GRAIN_PARTY_MONTH (
    party_id            VARCHAR NOT NULL,
    month_bucket        VARCHAR NOT NULL COMMENT 'YYYY-MM.',
    typology_id         VARCHAR,
    is_illicit_actual   BOOLEAN NOT NULL COMMENT 'True when any labelled illicit leg falls in this party-month.',
    illicit_leg_count   NUMBER(38,0) NOT NULL DEFAULT 0,
    total_leg_count     NUMBER(38,0) NOT NULL DEFAULT 0,
    expected_rule_ids   ARRAY   COMMENT 'Rules that SHOULD fire for this typology. Makes per-rule recall computable instead of requiring a hand-written mapping in the test.',
    built_at            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_eval_grain_party_month PRIMARY KEY (party_id, month_bucket)
)
COMMENT = 'Evaluation truth at party-month grain -- the declared denominator for precision and recall.';

-- --------------------------------------------------------------------------
-- Injected legs whose amounts are not yet known.
--
-- Structuring deposits are generated as a FRACTION of the CTR threshold, not
-- in rupees, so the threshold is resolved from POLICY_CLAUSE at load time rather
-- than baked into generated data. `load` promotes each row into
-- RAW.RAW_TRANSACTIONS with
--     amount_inr = ROUND(amount_sign * amount_ctr_ratio * threshold_lower, 2)
-- joining V_POLICY_CLAUSE_CURRENT on ctr_clause_id.
--
-- It lives HERE, not in RAW, because the ratio and cohort columns exist only on
-- injected rows: anywhere a consumer role could read them they would be a label.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS INJECTED_TXN_PENDING (
    txn_id                  VARCHAR         NOT NULL COMMENT 'Leg-level id; becomes RAW_TRANSACTIONS.txn_id on promotion.',
    txn_pair_id             VARCHAR         NOT NULL,
    leg_side                VARCHAR(1)      NOT NULL COMMENT 'D = debit (cash desk), C = credit (customer).',
    account_id              VARCHAR         NOT NULL,
    counterparty_account_id VARCHAR         NOT NULL,
    txn_timestamp           TIMESTAMP_NTZ   NOT NULL,
    amount_sign             NUMBER(1,0)     NOT NULL COMMENT '-1 for the debit leg, +1 for the credit leg.',
    amount_ctr_ratio        NUMBER(12,10)   NOT NULL COMMENT 'Unsigned fraction of the CTR threshold. Never copied into RAW.',
    ctr_clause_id           VARCHAR         NOT NULL COMMENT 'POLICY_CLAUSE id whose threshold_lower resolves the amount.',
    currency                VARCHAR         NOT NULL,
    txn_type                VARCHAR         NOT NULL,
    channel                 VARCHAR         NOT NULL,
    reference_text          VARCHAR,
    source_ref              VARCHAR         NOT NULL COMMENT 'Opaque UTR-like reference, same format as source legs. Copied to RAW on promotion, so it must not name the cohort.',
    cohort                  VARCHAR         NOT NULL COMMENT 'Injection cohort label, e.g. reskin_structuring.',
    generation_seed         NUMBER(38,0)    NOT NULL,
    resolved_at             TIMESTAMP_NTZ   COMMENT 'Set when load promotes the row. NULL = not yet in RAW_TRANSACTIONS.',
    inserted_at             TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_injected_txn_pending PRIMARY KEY (txn_id)
)
COMMENT = 'Injected legs awaiting threshold resolution from POLICY_CLAUSE. Admin-only: the ratio would reveal the injection.';

-- --------------------------------------------------------------------------
-- Parsed Patterns blocks, kept verbatim so a label dispute can be traced to the
-- source text rather than to the parser.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_LAUNDERING_PATTERNS (
    attempt_id      NUMBER(38,0) NOT NULL,
    typology        VARCHAR NOT NULL,
    max_detail      VARCHAR COMMENT 'e.g. "Max 16-degree Fan-Out" from the BEGIN delimiter.',
    txn_count       NUMBER(38,0) NOT NULL,
    txn_data        VARIANT NOT NULL COMMENT 'Array of the raw transaction rows in this attempt.',
    parsed_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_laundering_patterns PRIMARY KEY (attempt_id)
)
COMMENT = 'HI-Small_Patterns.txt, parsed. 370 attempts, 3,209 illicit transactions.';

-- --------------------------------------------------------------------------
-- Golden questions for the text-to-SQL suite.
--
-- Scored on RESULT match, not SQL-string match: two correct queries can differ
-- textually, and penalising that would measure phrasing rather than correctness.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS GOLDEN_QUESTIONS (
    question_id         VARCHAR NOT NULL,
    category            VARCHAR NOT NULL COMMENT 'TXN_LOOKUP | VELOCITY | THRESHOLD | NETWORK | SANCTIONS | CROSS_SILO | CASE_STATUS | REFUSAL.',
    question_text       VARCHAR NOT NULL,
    expected_sql        VARCHAR COMMENT 'Reference implementation. Used to compute the expected result, not compared textually against the agent''s SQL.',
    expected_result_hash VARCHAR COMMENT 'Canonical hash of the expected result set (risk_copilot.hashing).',
    expected_row_count  NUMBER(38,0),
    expects_refusal     BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'True for out-of-scope questions, where the correct behaviour is a clean refusal.',
    is_adversarial      BOOLEAN NOT NULL DEFAULT FALSE,
    notes               VARCHAR,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_golden_questions PRIMARY KEY (question_id)
)
COMMENT = 'Golden question set. Scored on result match; refusal cases are first-class, not omissions.';

CREATE TABLE IF NOT EXISTS EVAL_RUN (
    run_id          VARCHAR NOT NULL,
    suite_name      VARCHAR NOT NULL COMMENT 'DETECTION | TEXT_TO_SQL | GROUNDEDNESS | ADVERSARIAL | PIPELINE.',
    profile         VARCHAR NOT NULL,
    git_commit      VARCHAR COMMENT 'Ties a metric to the code that produced it. A number without a commit is not a result.',
    generation_seed NUMBER(38,0),
    model_versions  VARIANT,
    metrics         VARIANT NOT NULL COMMENT 'Suite-specific metrics, e.g. per-typology precision/recall/F1.',
    passed          BOOLEAN NOT NULL,
    started_at      TIMESTAMP_NTZ NOT NULL,
    finished_at     TIMESTAMP_NTZ,
    CONSTRAINT pk_eval_run PRIMARY KEY (run_id)
)
COMMENT = 'Evaluation run history, keyed to commit, seed and resolved model versions.';
