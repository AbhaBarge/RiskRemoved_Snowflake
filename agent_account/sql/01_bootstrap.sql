-- ===========================================================================
-- agent_account / 01_bootstrap.sql            (run as ACCOUNTADMIN, ONE TIME)
-- ===========================================================================
-- The agentic layer lives in a SEPARATE Snowflake account from the data.
--
--   data account   HTQQLHX-GI22223 (RISK_COPILOT_DEV, Cortex Analyst, the
--                  deterministic GATHER/DRAFT/REVIEW/ATTEST chain)
--   agent account  HQRWRXK-UZ26728 (this file): Cortex Agent, LLM drafter,
--                  cross-family LLM reviewer, PDF renderer
--
-- The two accounts are in different organisations and regions and both are
-- trial accounts, so neither data sharing nor external access integrations
-- are available. The bridge (src/risk_copilot/agentic) holds one key-pair
-- connection to each account and moves a hashed evidence package between them.
--
-- RC_AGENT_SVC is a SERVICE user (key-pair only, no password, no UI). Set its
-- key after running this file:
--   ALTER USER RC_AGENT_SVC SET RSA_PUBLIC_KEY = '<contents of keys/rc_agent_svc.pub>';
-- ===========================================================================
USE ROLE ACCOUNTADMIN;

CREATE DATABASE IF NOT EXISTS RISK_COPILOT_AGENT
    COMMENT = 'Risk Copilot agentic layer: STR drafting, review and PDF rendering over evidence from the data account';
CREATE SCHEMA IF NOT EXISTS RISK_COPILOT_AGENT.STR WITH MANAGED ACCESS;

CREATE WAREHOUSE IF NOT EXISTS RC_AGENT_WH
    WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE
    COMMENT = 'Risk Copilot agent tools';

CREATE ROLE IF NOT EXISTS RC_AGENT_ROLE COMMENT = 'Runs the STR agent and its tools. Never a human.';
GRANT ROLE RC_AGENT_ROLE TO ROLE SYSADMIN;

GRANT USAGE ON WAREHOUSE RC_AGENT_WH TO ROLE RC_AGENT_ROLE;
GRANT USAGE ON DATABASE RISK_COPILOT_AGENT TO ROLE RC_AGENT_ROLE;
GRANT USAGE ON SCHEMA RISK_COPILOT_AGENT.STR TO ROLE RC_AGENT_ROLE;
GRANT USE AI FUNCTIONS ON ACCOUNT TO ROLE RC_AGENT_ROLE;
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE RC_AGENT_ROLE;

CREATE USER IF NOT EXISTS RC_AGENT_SVC
    TYPE = SERVICE
    DEFAULT_ROLE = RC_AGENT_ROLE
    DEFAULT_WAREHOUSE = RC_AGENT_WH
    DEFAULT_NAMESPACE = RISK_COPILOT_AGENT.STR
    COMMENT = 'Risk Copilot bridge (Slack bot + Streamlit) -> STR agent';
GRANT ROLE RC_AGENT_ROLE TO USER RC_AGENT_SVC;
