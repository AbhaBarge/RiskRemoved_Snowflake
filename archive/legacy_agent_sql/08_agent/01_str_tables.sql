-- ===========================================================================
-- 08_agent / 01_str_tables.sql
-- ===========================================================================
-- Tables that store every STR request and every generated output. These form
-- the audit trail: what was asked, what was produced, and when.
--
-- STR_INPUT is append-only by discipline. STR_OUTPUT may have status FAILED
-- (Flaw 6 fix: a failed LLM call is recorded, not lost).
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

-- ---------------------------------------------------------------------------
-- STR Input -- every request the agent receives
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS STR_INPUT (
    str_input_id        VARCHAR         NOT NULL DEFAULT UUID_STRING(),
    -- Subject
    subject_name        VARCHAR(500)    NOT NULL,
    subject_type        VARCHAR(20)     NOT NULL,
    subject_id          VARCHAR(100),
    subject_address     VARCHAR(1000),
    subject_occupation  VARCHAR(200),
    -- Accounts
    accounts            VARIANT         NOT NULL
        COMMENT 'Array of {account_number, account_type, branch, opening_date}.',
    -- Transactions
    transactions        VARIANT         NOT NULL
        COMMENT 'Array of {date, amount, channel, from, to, description}.',
    total_amount        NUMBER(18,2),
    transaction_count   INTEGER,
    period_start        DATE,
    period_end          DATE,
    -- Suspicion
    suspicion_codes     ARRAY           NOT NULL
        COMMENT 'FIU-IND codes: A-F, Z.',
    suspicion_summary   VARCHAR(4000)   NOT NULL,
    -- Pattern
    typology            VARCHAR(50)
        COMMENT 'MULE_RING | STRUCTURING | PASSTHROUGH | SHELL_COMPANY | OTHER.',
    related_parties     VARIANT,
    -- Policy
    policy_clauses_cited ARRAY,
    -- Case link (NULL when the analyst supplies raw JSON instead of a case_id)
    case_id             VARCHAR
        COMMENT 'FK to CASE_RECORD when input was auto-assembled by collect_case_evidence.',
    -- Reporting entity
    bank_name           VARCHAR(200)    DEFAULT 'Demo Bank Ltd.',
    branch_name         VARCHAR(200),
    principal_officer   VARCHAR(200)    DEFAULT 'Priya Sharma, MLRO',
    -- Status
    status              VARCHAR(20)     NOT NULL DEFAULT 'DRAFT',
    -- Housekeeping
    created_at          TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    created_by          VARCHAR(100)    NOT NULL DEFAULT CURRENT_USER(),

    CONSTRAINT pk_str_input PRIMARY KEY (str_input_id)
)
COMMENT = 'Append-only log of every STR generation request.';

-- ---------------------------------------------------------------------------
-- STR Output -- every generated narrative (including failures)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS STR_OUTPUT (
    str_output_id       VARCHAR         NOT NULL DEFAULT UUID_STRING(),
    str_input_id        VARCHAR         NOT NULL,
    -- Generated content
    str_narrative       VARCHAR(16777216) NOT NULL
        COMMENT 'The full STR text. Contains GENERATION_FAILED prefix on error.',
    str_json            VARIANT         NOT NULL
        COMMENT 'Structured metadata. Contains error details on failure.',
    -- Computed values (Flaw 4 fix: numbers from SQL, not LLM)
    computed_values     VARIANT
        COMMENT 'Every figure the narrative cites, keyed by name, computed by SQL before the LLM call. The mechanism behind "no number in a filing comes from a language model".',
    -- Validation
    validation_status   VARCHAR(20)     NOT NULL DEFAULT 'PENDING',
    validation_errors   ARRAY,
    -- Status: DRAFT | PASSED | FAILED
    status              VARCHAR(20)     NOT NULL DEFAULT 'DRAFT',
    -- Model provenance
    model_used          VARCHAR(200)
        COMMENT 'The resolved Cortex model that generated this output.',
    prompt_version      VARCHAR(20)     DEFAULT 'v1',
    -- Housekeeping
    generated_at        TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    generated_by        VARCHAR(100)    NOT NULL DEFAULT 'AGENT:str_generator',

    CONSTRAINT pk_str_output PRIMARY KEY (str_output_id),
    CONSTRAINT fk_str_output_input FOREIGN KEY (str_input_id)
        REFERENCES STR_INPUT(str_input_id)
)
COMMENT = 'Every STR generation attempt, including failures.';
