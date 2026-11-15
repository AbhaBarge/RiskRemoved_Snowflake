-- ===========================================================================
-- procedures / 05_attest_str.sql
-- ===========================================================================
-- ATTEST_STR(artifact_id, decision, attestor, comment): the human gate.
--
-- Under PML Rule 7 (clause PRINCIPAL_OFFICER_ATTESTATION_REQUIRED) the
-- Principal Officer bears the filing obligation, so no STR reaches FILED
-- without a person signing this exact version. The rule is enforced here, in
-- code, never by an instruction to a model:
--
--   * EXECUTE AS CALLER, and the caller's session must hold the compliance
--     officer role -- the owner's role does not stand in for the caller's
--   * the artifact must be REVIEWED, its latest review APPROVE, and the hash
--     the Reviewer approved must equal the content's hash now; the signature
--     records that hash, so a later edit is detectable
--   * ATTESTED -> artifact FILED (a simulated FIU-IND submission), case CLOSED;
--     RETURNED -> artifact RETURNED, case back to DRAFTED for a redraft
--
-- No other procedure sets FILED. A refusal is logged as REFUSED with its reason.
-- ===========================================================================
{% from "procedures/_macros.sql" import audit with context %}
{% set T = tool %}
{% set EV = fq.evidence %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ T.attest_str }}(
    ARTIFACT_ID VARCHAR, DECISION VARCHAR, ATTESTOR VARCHAR, ATTEST_COMMENT VARCHAR DEFAULT NULL
)
    COPY GRANTS
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Human gate: Principal Officer attests or returns one REVIEWED STR. Caller rights; compliance role required.'
    EXECUTE AS CALLER
AS
$$
DECLARE
    bad_decision EXCEPTION (-20131, 'ATTEST_STR: decision must be ATTESTED or RETURNED');
    no_attestor EXCEPTION (-20132, 'ATTEST_STR: the attesting officer must be named');
    wrong_role EXCEPTION (-20133, 'ATTEST_STR: only the compliance officer role may attest');
    not_reviewed EXCEPTION (-20134, 'ATTEST_STR: only a REVIEWED artifact with an APPROVE review can be attested');
    hash_changed EXCEPTION (-20135, 'ATTEST_STR: the artifact changed after review; re-review before attesting');
    started TIMESTAMP_LTZ;
    run_at TIMESTAMP_NTZ;
    n INTEGER;
    v_case VARCHAR;
    fid VARCHAR;
    v_status VARCHAR;
    stored_hash VARCHAR;
    actual_hash VARCHAR;
    reviewed_hash VARCHAR;
    last_decision VARCHAR;
    att_id VARCHAR;
    new_status VARCHAR;
    err VARCHAR;
BEGIN
    started := CURRENT_TIMESTAMP();
    fid := (SELECT MAX(finding_id) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    v_case := (SELECT MAX(case_id) FROM {{ EV }}.FINDING WHERE finding_id = :fid);
    IF (decision IS NULL OR decision NOT IN ('ATTESTED', 'RETURNED')) THEN
        RAISE bad_decision;
    END IF;
    IF (attestor IS NULL OR LENGTH(TRIM(attestor)) < 3) THEN
        RAISE no_attestor;
    END IF;
    IF (NOT IS_ROLE_IN_SESSION('{{ role.compliance_officer }}')) THEN
        RAISE wrong_role;
    END IF;

    v_status := (SELECT MAX(status) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    last_decision := (
        SELECT MAX(decision) FROM (
            SELECT decision FROM {{ EV }}.REVIEW WHERE finding_id = :fid
            QUALIFY ROW_NUMBER() OVER (ORDER BY reviewed_at DESC, review_id DESC) = 1
        )
    );
    IF (v_status IS DISTINCT FROM 'REVIEWED' OR last_decision IS DISTINCT FROM 'APPROVE') THEN
        RAISE not_reviewed;
    END IF;
    stored_hash := (SELECT MAX(content_hash) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    actual_hash := (SELECT MAX(SHA2(TO_JSON(content), 256)) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    reviewed_hash := (
        SELECT MAX(checks_run:content_hash::VARCHAR) FROM (
            SELECT checks_run FROM {{ EV }}.REVIEW WHERE finding_id = :fid
            QUALIFY ROW_NUMBER() OVER (ORDER BY reviewed_at DESC, review_id DESC) = 1
        )
    );
    IF (stored_hash IS DISTINCT FROM actual_hash OR reviewed_hash IS DISTINCT FROM actual_hash) THEN
        RAISE hash_changed;
    END IF;

    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    n := (SELECT COUNT(*) FROM {{ EV }}.ATTESTATION WHERE artifact_id = :artifact_id);
    att_id := 'ATT-' || LEFT(SHA2(artifact_id || '|' || TO_VARCHAR(n + 1), 256), 16);
    INSERT INTO {{ EV }}.ATTESTATION (
        attestation_id, artifact_id, attested_content_hash, attestor, attestor_role, decision, comment
    )
    SELECT :att_id, :artifact_id, :actual_hash, TRIM(:attestor), 'PRINCIPAL_OFFICER', :decision, :attest_comment;

    new_status := IFF(decision = 'ATTESTED', 'FILED', 'RETURNED');
    UPDATE {{ EV }}.ARTIFACT SET status = :new_status WHERE artifact_id = :artifact_id;
    UPDATE {{ EV }}.CASE_RECORD
    SET status = IFF(:decision = 'ATTESTED', 'CLOSED', 'DRAFTED'),
        closed_at = IFF(:decision = 'ATTESTED', :run_at, NULL),
        closure_reason = IFF(:decision = 'ATTESTED', 'STR_FILED', NULL),
        updated_at = :run_at
    WHERE case_id = :v_case;

    {{ audit("REVIEWER", T.attest_str, "OBJECT_CONSTRUCT('artifact_id', :artifact_id, 'decision', :decision, 'attestor', :attestor)", "SUCCESS",
             "'HUMAN'", "'n/a'", case_var="v_case") }};
    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'attestation_id', att_id,
        'artifact_id', artifact_id,
        'artifact_status', new_status,
        'attested_content_hash', actual_hash,
        'decision', decision
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        err := SQLERRM;
        -- The refusal itself is evidence: record who tried and why it failed.
        {{ audit("REVIEWER", T.attest_str, "OBJECT_CONSTRUCT('artifact_id', :artifact_id, 'decision', :decision, 'attestor', :attestor)", "REFUSED",
                 "'HUMAN'", "'n/a'", error=":err", case_var="v_case") }};
        RAISE;
END;
$$;
