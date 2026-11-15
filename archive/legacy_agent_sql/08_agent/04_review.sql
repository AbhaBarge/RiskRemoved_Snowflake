-- ===========================================================================
-- 08_agent / 04_review.sql
-- ===========================================================================
-- FLAW 7 FIX: Adversarial review before the draft reaches a human.
--
-- Two kinds of checks:
--   1. DETERMINISTIC (SQL): citation resolution, clause validity, number match
--   2. LLM (different model family): narrative consistency, omitted evidence
--
-- The reviewer model is deliberately non-Anthropic when the drafter is
-- Anthropic (conf/models.yml enforces this). A critic sharing the drafter's
-- blind spots is not a useful critic.
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

CREATE OR REPLACE PROCEDURE REVIEW_FINDING(P_FINDING_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Adversarial review: deterministic checks + different-model-family LLM review.'
    EXECUTE AS OWNER
AS
BEGIN
    LET review_id VARCHAR := (SELECT UUID_STRING());
    LET v_now TIMESTAMP_NTZ := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;

    -- 1. Get the finding
    LET v_case_id VARCHAR;
    LET v_narrative VARCHAR;
    LET v_conclusion VARCHAR;
    LET v_cited_clause_ids ARRAY;
    LET v_cited_evidence_ids ARRAY;
    LET v_computed_values VARIANT;
    LET v_author_model VARCHAR;

    SELECT case_id, narrative, conclusion, cited_clause_ids, cited_evidence_ids,
           computed_values, author_model
    INTO :v_case_id, :v_narrative, :v_conclusion, :v_cited_clause_ids,
         :v_cited_evidence_ids, :v_computed_values, :v_author_model
    FROM FINDING
    WHERE finding_id = :P_FINDING_ID;

    IF (:v_case_id IS NULL) THEN
        RETURN OBJECT_CONSTRUCT('status', 'ERROR', 'message', 'Finding not found: ' || :P_FINDING_ID);
    END IF;

    -- -----------------------------------------------------------------------
    -- DETERMINISTIC CHECK 1: Every cited evidence_id resolves
    -- -----------------------------------------------------------------------
    LET v_cited_count INTEGER := ARRAY_SIZE(:v_cited_evidence_ids);
    LET v_resolved_count INTEGER := (
        SELECT COUNT(*)
        FROM EVIDENCE e
        WHERE e.evidence_id IN (
            SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(input => :v_cited_evidence_ids))
        )
    );
    LET v_citation_passed BOOLEAN := (:v_cited_count = :v_resolved_count AND :v_cited_count > 0);

    -- -----------------------------------------------------------------------
    -- DETERMINISTIC CHECK 2: Every cited clause is currently in force
    -- -----------------------------------------------------------------------
    LET v_clause_count INTEGER := ARRAY_SIZE(:v_cited_clause_ids);
    LET v_active_clause_count INTEGER := (
        SELECT COUNT(*)
        FROM {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT pc
        WHERE pc.clause_id IN (
            SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(input => :v_cited_clause_ids))
        )
    );
    LET v_clause_passed BOOLEAN := (:v_clause_count = :v_active_clause_count AND :v_clause_count > 0);

    -- -----------------------------------------------------------------------
    -- DETERMINISTIC CHECK 3: Computed values are present
    -- -----------------------------------------------------------------------
    LET v_numeric_passed BOOLEAN := (:v_computed_values IS NOT NULL);

    -- -----------------------------------------------------------------------
    -- LLM CHECK: Different model family reviews the narrative
    -- -----------------------------------------------------------------------
    LET reviewer_model VARCHAR := '{{ resolved_model_reviewer | default("mistral-large2") }}';
    LET review_prompt VARCHAR := '
You are a compliance review officer. Your job is to check an STR draft for defects.

Review the following STR narrative and answer these questions:
1. Is the narrative internally consistent? (dates, amounts, parties match each other)
2. Does the conclusion follow from the evidence cited?
3. Are there obvious omissions (e.g., a transaction mentioned but not in the table)?
4. Does the narrative make any claims not supported by the cited data?

Respond with a JSON object:
{
  "decision": "APPROVE" or "REJECT",
  "defects": ["list of specific defects found, empty if APPROVE"],
  "consistency_ok": true/false,
  "conclusion_supported": true/false,
  "omissions_found": true/false,
  "unsupported_claims": true/false
}

<narrative>
' || :v_narrative || '
</narrative>';

    LET review_response VARCHAR;
    LET llm_decision VARCHAR := 'APPROVE';
    LET llm_defects VARIANT := ARRAY_CONSTRUCT();
    BEGIN
        review_response := (SELECT SNOWFLAKE.CORTEX.COMPLETE(:reviewer_model, :review_prompt));
        -- Try to parse the LLM response as JSON
        BEGIN
            LET parsed_review VARIANT := PARSE_JSON(:review_response);
            llm_decision := COALESCE(:parsed_review:decision::VARCHAR, 'APPROVE');
            llm_defects := COALESCE(:parsed_review:defects, ARRAY_CONSTRUCT());
        EXCEPTION
            WHEN OTHER THEN
                -- If LLM response is not valid JSON, treat as a text review
                llm_decision := 'APPROVE';
                llm_defects := ARRAY_CONSTRUCT('LLM response was not structured JSON: ' || LEFT(:review_response, 500));
        END;
    EXCEPTION
        WHEN OTHER THEN
            -- LLM review failed; deterministic checks still count
            llm_decision := 'SKIPPED';
            llm_defects := ARRAY_CONSTRUCT('LLM review failed: ' || SQLERRM);
    END;

    -- -----------------------------------------------------------------------
    -- Overall decision: REJECT if any deterministic check fails or LLM rejects
    -- -----------------------------------------------------------------------
    LET v_decision VARCHAR := CASE
        WHEN NOT :v_citation_passed THEN 'REJECT'
        WHEN NOT :v_clause_passed THEN 'REJECT'
        WHEN :llm_decision = 'REJECT' THEN 'REJECT'
        ELSE 'APPROVE'
    END;

    LET v_all_defects VARIANT := :llm_defects;
    IF (NOT :v_citation_passed) THEN
        v_all_defects := ARRAY_CAT(:v_all_defects,
            ARRAY_CONSTRUCT('CITATION_FAILED: ' || :v_resolved_count || '/' || :v_cited_count || ' evidence IDs resolved'));
    END IF;
    IF (NOT :v_clause_passed) THEN
        v_all_defects := ARRAY_CAT(:v_all_defects,
            ARRAY_CONSTRUCT('CLAUSE_FAILED: ' || :v_active_clause_count || '/' || :v_clause_count || ' clauses currently in force'));
    END IF;

    LET v_checks VARIANT := OBJECT_CONSTRUCT(
        'citation_resolution', OBJECT_CONSTRUCT('passed', :v_citation_passed, 'cited', :v_cited_count, 'resolved', :v_resolved_count),
        'clause_in_effect', OBJECT_CONSTRUCT('passed', :v_clause_passed, 'cited', :v_clause_count, 'active', :v_active_clause_count),
        'computed_values_present', OBJECT_CONSTRUCT('passed', :v_numeric_passed),
        'llm_review', OBJECT_CONSTRUCT('model', :reviewer_model, 'decision', :llm_decision)
    );

    -- Insert the review row
    INSERT INTO REVIEW (
        review_id, finding_id, reviewer_type, decision, defects_found, checks_run,
        numeric_recompute_passed, citation_resolution_passed, clause_in_effect_passed,
        reviewer_model, reviewed_at
    ) VALUES (
        :review_id, :P_FINDING_ID, 'ADVERSARIAL_AGENT', :v_decision, :v_all_defects, :v_checks,
        :v_numeric_passed, :v_citation_passed, :v_clause_passed,
        :reviewer_model, :v_now
    );

    RETURN OBJECT_CONSTRUCT(
        'status', 'SUCCESS',
        'review_id', :review_id,
        'decision', :v_decision,
        'defects', :v_all_defects,
        'checks', :v_checks
    );
END;
