-- ===========================================================================
-- 11_app_access / 01_public_app.sql
-- ===========================================================================
-- What the hosted app (Streamlit Community Cloud) may do, as a role.
--
-- The hosted app connects as one service user (conf/base.yml:app.public_user,
-- created by `riskcopilot app public-access` with a key pair, no password).
-- Its role can:
--   * read the demo: curated, governance, evidence and the audit log, through
--     the compliance and service roles it inherits, plus RAW_ACCOUNTS;
--   * run the case tools: USAGE on the five procedures. ATTEST_STR runs with
--     caller's rights, so this role holds the compliance role it checks for,
--     and the service role's INSERT / UPDATE on the evidence tables;
--   * ask Cortex Analyst: SELECT on the semantic view.
-- It cannot read GROUND_TRUTH. The app's recall / precision come from
-- V_EVAL_SCORES, a secure view of run-level aggregates (obligation 4: no label
-- is reachable outside the admin role).
--
-- Applied after `procedures` and `semantic`, whose objects it grants on.
-- ===========================================================================
{% set T = tool %}
{% set PUB = role.public_app %}

-- Run-level scores only: no per-party, per-transaction or label value leaves.
CREATE OR REPLACE SECURE VIEW {{ fq.curated }}.{{ view.eval_scores }}
    COMMENT = 'Detection scores per EVAL_RUN, aggregates only. The app reads this, never GROUND_TRUTH.'
AS
SELECT run_id, suite_name, profile, git_commit, generation_seed, passed, finished_at,
       metrics:detection AS detection,
       metrics:hero:status::STRING AS hero_status
FROM {{ fq.ground_truth }}.EVAL_RUN;

{% if PUB %}
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS {{ PUB }}
    COMMENT = 'The hosted demo app ({{ profile }}): reads the demo, runs the case tools. No GROUND_TRUTH.';
GRANT ROLE {{ PUB }} TO ROLE {{ role.admin }};
GRANT ROLE {{ role.compliance_officer }} TO ROLE {{ PUB }};
GRANT ROLE {{ role.service }} TO ROLE {{ PUB }};
GRANT USAGE ON WAREHOUSE {{ app.warehouse }} TO ROLE {{ PUB }};

USE ROLE {{ role.admin }};

GRANT USAGE ON SCHEMA {{ fq.raw }} TO ROLE {{ PUB }};
GRANT SELECT ON TABLE {{ fq.raw }}.RAW_ACCOUNTS TO ROLE {{ PUB }};
GRANT SELECT ON VIEW {{ fq.curated }}.{{ view.eval_scores }} TO ROLE {{ PUB }};
GRANT SELECT ON SEMANTIC VIEW {{ fq.curated }}.{{ cortex.semantic_view }} TO ROLE {{ PUB }};

GRANT USAGE ON PROCEDURE {{ fq.evidence }}.{{ T.gather_evidence }}(VARCHAR) TO ROLE {{ PUB }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.{{ T.draft_str }}(VARCHAR, BOOLEAN) TO ROLE {{ PUB }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.{{ T.review_str }}(VARCHAR) TO ROLE {{ PUB }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.{{ T.attest_str }}(VARCHAR, VARCHAR, VARCHAR, VARCHAR) TO ROLE {{ PUB }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.{{ T.reset_cases }}(VARCHAR, BOOLEAN) TO ROLE {{ PUB }};
{% endif %}
