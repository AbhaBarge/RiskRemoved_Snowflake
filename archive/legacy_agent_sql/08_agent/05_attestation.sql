-- ===========================================================================
-- 08_agent / 05_attestation.sql
-- ===========================================================================
-- Creates an ARTIFACT from an approved FINDING and routes it to the MLRO.
--
-- This procedure does NOT insert into ATTESTATION. Only the
-- COMPLIANCE_OFFICER role has INSERT privilege on that table.
-- The agent runs as RC_SERVICE_ROLE, which cannot attest.
-- This is the structural human gate (PMLA Rule 7).
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

CREATE OR REPLACE PROCEDURE REQUEST_ATTESTATION(P_FINDING_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Creates an ARTIFACT from an approved finding. Does NOT attest -- only COMPLIANCE_OFFICER can.'
    EXECUTE AS OWNER
AS
BEGIN
    LET artifact_id VARCHAR := (SELECT UUID_STRING());
    LET v_now TIMESTAMP_NTZ := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;

    -- 1. Verify the finding exists
    LET v_case_id VARCHAR;
    LET v_narrative VARCHAR;
    LET v_conclusion VARCHAR;
    LET v_computed_values VARIANT;

    SELECT case_id, narrative, conclusion, computed_values
    INTO :v_case_id, :v_narrative, :v_conclusion, :v_computed_values
    FROM FINDING
    WHERE finding_id = :P_FINDING_ID;

    IF (:v_case_id IS NULL) THEN
        RETURN OBJECT_CONSTRUCT('status', 'ERROR', 'message', 'Finding not found: ' || :P_FINDING_ID);
    END IF;

    -- 2. Verify the finding has a passing review
    LET v_review_decision VARCHAR := (
        SELECT decision
        FROM REVIEW
        WHERE finding_id = :P_FINDING_ID
        ORDER BY reviewed_at DESC
        LIMIT 1
    );

    IF (:v_review_decision IS NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'status', 'ERROR',
            'message', 'Finding has no review. Call review_finding first.'
        );
    END IF;
    IF (:v_review_decision <> 'APPROVE') THEN
        RETURN OBJECT_CONSTRUCT(
            'status', 'ERROR',
            'message', 'Finding was rejected by review. Fix the defects and create a new finding version.',
            'review_decision', :v_review_decision
        );
    END IF;

    -- 3. Assemble the artifact content
    LET v_content VARIANT := OBJECT_CONSTRUCT(
        'finding_id', :P_FINDING_ID,
        'case_id', :v_case_id,
        'narrative', :v_narrative,
        'conclusion', :v_conclusion,
        'computed_values', :v_computed_values,
        'assembled_at', :v_now::VARCHAR
    );

    -- 4. Compute the content hash (binds the attestation to this exact version)
    LET v_content_hash VARCHAR := (SELECT SHA2(:v_content::VARCHAR, 256));

    -- 5. Insert the artifact
    INSERT INTO ARTIFACT (
        artifact_id, finding_id, artifact_type, version,
        content, content_hash, status, validation_status, created_at
    ) VALUES (
        :artifact_id, :P_FINDING_ID, 'STR', 1,
        :v_content, :v_content_hash, 'DRAFT', 'PASS', :v_now
    );

    -- 6. Update case status to DRAFTED
    UPDATE CASE_RECORD
    SET status = 'DRAFTED', updated_at = :v_now
    WHERE case_id = :v_case_id
      AND status IN ('OPEN', 'INVESTIGATING');

    RETURN OBJECT_CONSTRUCT(
        'status', 'SUCCESS',
        'artifact_id', :artifact_id,
        'content_hash', :v_content_hash,
        'case_id', :v_case_id,
        'next_steps', ARRAY_CONSTRUCT(
            'The STR artifact is now in DRAFT status.',
            'The MLRO (COMPLIANCE_OFFICER role) must review and attest.',
            'To attest, the MLRO runs:',
            'INSERT INTO ATTESTATION (attestation_id, artifact_id, attested_content_hash, attestor, attestor_role, decision)',
            'VALUES (UUID_STRING(), ''' || :artifact_id || ''', ''' || :v_content_hash || ''', CURRENT_USER(), ''PRINCIPAL_OFFICER'', ''ATTESTED'')',
            'The content_hash must match -- a post-attestation edit will be caught.'
        )
    );
END;
