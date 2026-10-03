-- ===========================================================================
-- agent_account / 03_tools.sql                       (run as RC_AGENT_ROLE)
-- ===========================================================================
-- The STR agent's four tools. Each is a thin handler into
-- agent_account/python/str_tools.py, which deploy.py stages to @CODE first.
--
--   GET_EVIDENCE_PACKAGE  what the agent may rely on for one package
--   DRAFT_STR_NARRATIVE   maker: Anthropic model writes the FIU-IND sections
--   REVIEW_STR_DRAFT      checker: deterministic figure check + non-Anthropic
--                         model; fails closed
--   RENDER_STR_PDF        the final report on @STR_REPORTS
-- ===========================================================================
USE SCHEMA RISK_COPILOT_AGENT.STR;

CREATE OR REPLACE PROCEDURE GET_EVIDENCE_PACKAGE(PACKAGE_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@RISK_COPILOT_AGENT.STR.CODE/str_tools.py')
    HANDLER = 'str_tools.get_package'
    COMMENT = 'STR agent tool: the evidence facts for one package (subject, figures, signals, clauses, latest draft)'
    EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE DRAFT_STR_NARRATIVE(PACKAGE_ID VARCHAR, REVISION_NOTES VARCHAR DEFAULT NULL)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@RISK_COPILOT_AGENT.STR.CODE/str_tools.py')
    HANDLER = 'str_tools.draft'
    COMMENT = 'STR agent tool (maker): LLM drafts FIU-IND STR sections, quoting evidence figures only'
    EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE REVIEW_STR_DRAFT(DRAFT_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@RISK_COPILOT_AGENT.STR.CODE/str_tools.py')
    HANDLER = 'str_tools.review'
    COMMENT = 'STR agent tool (checker): deterministic figure trace + cross-family LLM review. Fails closed.'
    EXECUTE AS OWNER;

CREATE OR REPLACE PROCEDURE RENDER_STR_PDF(
    DRAFT_ID VARCHAR,
    TARGET_ARTIFACT_ID VARCHAR DEFAULT NULL,
    TARGET_CONTENT_HASH VARCHAR DEFAULT NULL,
    DATA_REVIEW_JSON VARCHAR DEFAULT NULL
)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python', 'reportlab')
    IMPORTS = ('@RISK_COPILOT_AGENT.STR.CODE/str_tools.py')
    HANDLER = 'str_tools.render_pdf'
    COMMENT = 'STR agent tool: renders the FIU-IND STR PDF to @STR_REPORTS'
    EXECUTE AS OWNER;
