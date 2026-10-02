-- ===========================================================================
-- procedures / 06_reset_cases.sql
-- ===========================================================================
-- RESET_CASES(via, dry_run): put every case back where ROUTE_SIGNALS left it,
-- so the demo can run again on the same cases.
--
-- One transaction:
--   * delete the case workflow's rows, children first: ATTESTATION, REVIEW,
--     ARTIFACT, FINDING, EVIDENCE_QUERY_LOG, EVIDENCE
--   * reopen every case: OPEN, and clear the suspicion clock, the SLA and the
--     closure that the procedures set
--   * log one OPERATOR row in the audit log, with the counts and case ids
--
-- SIGNAL and CASE_SIGNAL are detection's output and stay. The audit log stays,
-- so the trail shows the earlier runs and the reset that wiped them. This
-- breaks the evidence chain's append-only rule on purpose: a demo tool only.
--
-- dry_run = TRUE counts what a reset would delete and changes nothing. `via`
-- records where the reset came from (APP, CLI, SQL).
-- ===========================================================================
{% set T = tool %}
{% set EV = fq.evidence %}
{% set untouched = "status = 'OPEN' AND suspicion_established_at IS NULL AND sla_deadline IS NULL AND closed_at IS NULL AND closure_reason IS NULL" %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ T.reset_cases }}(VIA VARCHAR DEFAULT 'SQL', DRY_RUN BOOLEAN DEFAULT FALSE)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Demo reset: deletes the case workflow rows and reopens every case. Keeps signals and the audit log.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    n_att INTEGER;
    n_rev INTEGER;
    n_art INTEGER;
    n_fnd INTEGER;
    n_qlg INTEGER;
    n_evd INTEGER;
    reopened ARRAY;
    counts OBJECT;
BEGIN
    BEGIN TRANSACTION;
    n_att := (SELECT COUNT(*) FROM {{ EV }}.ATTESTATION);
    n_rev := (SELECT COUNT(*) FROM {{ EV }}.REVIEW);
    n_art := (SELECT COUNT(*) FROM {{ EV }}.ARTIFACT);
    n_fnd := (SELECT COUNT(*) FROM {{ EV }}.FINDING);
    n_qlg := (SELECT COUNT(*) FROM {{ EV }}.EVIDENCE_QUERY_LOG);
    n_evd := (SELECT COUNT(*) FROM {{ EV }}.EVIDENCE);
    reopened := (
        SELECT COALESCE(ARRAY_AGG(case_id) WITHIN GROUP (ORDER BY case_id), ARRAY_CONSTRUCT())
        FROM {{ EV }}.CASE_RECORD
        WHERE NOT ({{ untouched }})
    );
    counts := OBJECT_CONSTRUCT('ATTESTATION', n_att, 'REVIEW', n_rev, 'ARTIFACT', n_art, 'FINDING', n_fnd, 'EVIDENCE_QUERY_LOG', n_qlg, 'EVIDENCE', n_evd);

    IF (dry_run) THEN
        ROLLBACK;
        RETURN OBJECT_CONSTRUCT('dry_run', TRUE, 'rows', counts, 'cases_reopened', ARRAY_SIZE(reopened), 'case_ids', reopened);
    END IF;

    DELETE FROM {{ EV }}.ATTESTATION;
    DELETE FROM {{ EV }}.REVIEW;
    DELETE FROM {{ EV }}.ARTIFACT;
    DELETE FROM {{ EV }}.FINDING;
    DELETE FROM {{ EV }}.EVIDENCE_QUERY_LOG;
    DELETE FROM {{ EV }}.EVIDENCE;

    UPDATE {{ EV }}.CASE_RECORD
    SET status = 'OPEN', suspicion_established_at = NULL, sla_deadline = NULL,
        closed_at = NULL, closure_reason = NULL,
        updated_at = CURRENT_TIMESTAMP()::TIMESTAMP_NTZ
    WHERE NOT ({{ untouched }});

    INSERT INTO {{ fq.audit }}.AGENT_ACTION_LOG (
        log_id, session_id, turn_number, agent_mode, invoked_by, snowflake_user, user_role,
        tools_invoked, tool_arguments, case_ids_touched, model_version, prompt_version,
        rows_touched, outcome
    )
    SELECT 'LOG-' || UUID_STRING(), CURRENT_SESSION()::VARCHAR,
           (SELECT COUNT(*) + 1 FROM {{ fq.audit }}.AGENT_ACTION_LOG WHERE session_id = CURRENT_SESSION()::VARCHAR),
           'OPERATOR', 'USER', CURRENT_USER(), CURRENT_ROLE(),
           ARRAY_CONSTRUCT('{{ T.reset_cases }}'), OBJECT_CONSTRUCT('via', :via, 'deleted', :counts), :reopened,
           'n/a', 'n/a',
           :n_att + :n_rev + :n_art + :n_fnd + :n_qlg + :n_evd + ARRAY_SIZE(:reopened), 'SUCCESS';
    COMMIT;
    RETURN OBJECT_CONSTRUCT('dry_run', FALSE, 'rows', counts, 'cases_reopened', ARRAY_SIZE(reopened), 'case_ids', reopened);
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
