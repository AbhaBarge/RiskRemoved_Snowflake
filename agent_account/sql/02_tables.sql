-- ===========================================================================
-- agent_account / 02_tables.sql                      (run as RC_AGENT_ROLE)
-- ===========================================================================
-- What the agent account keeps. Evidence still lives in the data account; an
-- EVIDENCE_PACKAGE is a hashed, read-only copy of exactly what the agent was
-- shown, with pointers (query_log_id + result_hash) back to the source logs.
--
--   EVIDENCE_PACKAGE  one per pipeline run: case, subject (PAN masked),
--                     computed_values, deposits, signals, clauses, query-log
--                     pointers, Cortex Analyst context
--   AI_DRAFT          the LLM drafter's FIU-IND sections for a package
--   AI_REVIEW         numeric check + cross-family LLM verdict for a draft
--   STR_REPORT        the rendered PDF on @STR_REPORTS and what it attests to
--   AGENT_RUN_LOG     one row per bridge run, for latency and failure triage
-- ===========================================================================
USE ROLE ACCOUNTADMIN;
GRANT CREATE TABLE, CREATE PROCEDURE, CREATE FUNCTION, CREATE STAGE, CREATE AGENT
    ON SCHEMA RISK_COPILOT_AGENT.STR TO ROLE RC_AGENT_ROLE;

USE ROLE RC_AGENT_ROLE;
USE SCHEMA RISK_COPILOT_AGENT.STR;

CREATE TABLE IF NOT EXISTS EVIDENCE_PACKAGE (
    package_id          VARCHAR NOT NULL PRIMARY KEY,
    case_id             VARCHAR NOT NULL,
    source_account      VARCHAR NOT NULL,
    source_database     VARCHAR NOT NULL,
    source_artifact_id  VARCHAR NOT NULL,
    source_content_hash VARCHAR NOT NULL,
    payload             VARIANT NOT NULL,
    payload_hash        VARCHAR NOT NULL,
    created_by          VARCHAR DEFAULT CURRENT_USER(),
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
) COMMENT = 'Hashed evidence copied from the data account for one STR run';

CREATE TABLE IF NOT EXISTS AI_DRAFT (
    draft_id             VARCHAR NOT NULL PRIMARY KEY,
    package_id           VARCHAR NOT NULL,
    attempt              INTEGER NOT NULL,
    sections             VARIANT,
    grounds_of_suspicion VARCHAR,
    revision_notes       VARCHAR,
    model                VARCHAR NOT NULL,
    prompt_version       VARCHAR NOT NULL,
    status               VARCHAR NOT NULL,
    error                VARCHAR,
    created_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
) COMMENT = 'LLM drafter output: FIU-IND STR sections for one evidence package';

CREATE TABLE IF NOT EXISTS AI_REVIEW (
    review_id      VARCHAR NOT NULL PRIMARY KEY,
    draft_id       VARCHAR NOT NULL,
    numeric_check  VARIANT,
    llm_verdict    VARIANT,
    decision       VARCHAR NOT NULL,
    defects        ARRAY,
    reviewer_model VARCHAR NOT NULL,
    created_at     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
) COMMENT = 'Checker: deterministic figure check + non-Anthropic LLM verdict. Fails closed.';

CREATE TABLE IF NOT EXISTS STR_REPORT (
    report_id           VARCHAR NOT NULL PRIMARY KEY,
    draft_id            VARCHAR NOT NULL,
    case_id             VARCHAR NOT NULL,
    target_artifact_id  VARCHAR,
    target_content_hash VARCHAR,
    stage_path          VARCHAR NOT NULL,
    pdf_sha256          VARCHAR NOT NULL,
    created_at          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
) COMMENT = 'Rendered STR PDFs on @STR_REPORTS';

CREATE TABLE IF NOT EXISTS AGENT_RUN_LOG (
    run_id       VARCHAR NOT NULL PRIMARY KEY,
    case_id      VARCHAR,
    channel      VARCHAR,
    requested_by VARCHAR,
    status       VARCHAR NOT NULL,
    steps        VARIANT,
    result       VARIANT,
    error        VARCHAR,
    started_at   TIMESTAMP_NTZ,
    finished_at  TIMESTAMP_NTZ
) COMMENT = 'One row per bridge pipeline run (Slack or Streamlit)';

CREATE STAGE IF NOT EXISTS STR_REPORTS
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    DIRECTORY = (ENABLE = TRUE)
    COMMENT = 'Rendered STR PDFs';
