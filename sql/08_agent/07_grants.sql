-- ===========================================================================
-- 08_agent / 07_grants.sql
-- ===========================================================================
-- Role grants for all agent objects. Follows the three-role model:
--
--   RC_SERVICE_ROLE:      Runs the agent, calls procedures, reads/writes
--                         STR_INPUT, STR_OUTPUT, EVIDENCE, FINDING, REVIEW,
--                         ARTIFACT. CANNOT write ATTESTATION.
--
--   COMPLIANCE_OFFICER:   Everything RC_SERVICE_ROLE can do, plus INSERT on
--                         ATTESTATION (the human gate).
--
--   BRANCH_ANALYST_*:     Can read evidence for their branch only (row access
--                         policy on CASE_RECORD). Cannot run the agent.
-- ===========================================================================

-- Agent usage
GRANT USAGE ON AGENT {{ fq.evidence }}.{{ cortex.agent }}
    TO ROLE {{ role.service }};
GRANT USAGE ON AGENT {{ fq.evidence }}.{{ cortex.agent }}
    TO ROLE {{ role.compliance_officer }};

-- Procedures (EXECUTE privilege)
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.COLLECT_CASE_EVIDENCE(VARCHAR)
    TO ROLE {{ role.service }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.GENERATE_STR(VARCHAR)
    TO ROLE {{ role.service }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.REVIEW_FINDING(VARCHAR)
    TO ROLE {{ role.service }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.REQUEST_ATTESTATION(VARCHAR)
    TO ROLE {{ role.service }};

-- Functions
GRANT USAGE ON FUNCTION {{ fq.evidence }}.VALIDATE_STR_INPUTS(VARIANT)
    TO ROLE {{ role.service }};

-- Tables the agent writes to (via EXECUTE AS OWNER procedures, but the
-- service role also needs SELECT for ad-hoc queries)
GRANT SELECT ON TABLE {{ fq.evidence }}.STR_INPUT TO ROLE {{ role.service }};
GRANT SELECT ON TABLE {{ fq.evidence }}.STR_OUTPUT TO ROLE {{ role.service }};

-- Compliance officer gets everything the service role has, plus ATTESTATION
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.COLLECT_CASE_EVIDENCE(VARCHAR)
    TO ROLE {{ role.compliance_officer }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.GENERATE_STR(VARCHAR)
    TO ROLE {{ role.compliance_officer }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.REVIEW_FINDING(VARCHAR)
    TO ROLE {{ role.compliance_officer }};
GRANT USAGE ON PROCEDURE {{ fq.evidence }}.REQUEST_ATTESTATION(VARCHAR)
    TO ROLE {{ role.compliance_officer }};
GRANT USAGE ON FUNCTION {{ fq.evidence }}.VALIDATE_STR_INPUTS(VARIANT)
    TO ROLE {{ role.compliance_officer }};
GRANT SELECT ON TABLE {{ fq.evidence }}.STR_INPUT TO ROLE {{ role.compliance_officer }};
GRANT SELECT ON TABLE {{ fq.evidence }}.STR_OUTPUT TO ROLE {{ role.compliance_officer }};

-- THE HUMAN GATE: only COMPLIANCE_OFFICER can insert attestations
GRANT INSERT ON TABLE {{ fq.evidence }}.ATTESTATION
    TO ROLE {{ role.compliance_officer }};
GRANT SELECT ON TABLE {{ fq.evidence }}.ATTESTATION
    TO ROLE {{ role.compliance_officer }};
