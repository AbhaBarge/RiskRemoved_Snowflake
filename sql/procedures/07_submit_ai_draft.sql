-- ===========================================================================
-- procedures / 07_submit_ai_draft.sql
-- ===========================================================================
-- SUBMIT_AI_DRAFT(base_artifact_id, grounds, author_model, agent_review,
-- prompt_version): the write-back for an STR narrative drafted by the Cortex
-- Agent in the separate agent account (agent_account/, driven by the bridge in
-- src/risk_copilot/agentic).
--
-- The agent only writes prose. Everything REVIEW_STR checks is carried over
-- unchanged from the base version that DRAFT_STR wrote from logged evidence:
-- computed_values (each tied to its query_log_id), cited clauses, cited
-- evidence, subject and transactions. So the new version passes or fails
-- REVIEW_STR on the same terms as a template draft, and attestation still needs
-- the Principal Officer.
--
-- Like a redraft, it never overwrites: the AI narrative is FINDING version n+1,
-- the base becomes SUPERSEDED, and author_model names the drafting model.
-- agent_review keeps the agent account's own verdict (cross-family LLM review
-- and figure trace) inside the hashed artifact content.
-- ===========================================================================
{% from "procedures/_macros.sql" import audit with context %}
{% set T = tool %}
{% set EV = fq.evidence %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ T.submit_ai_draft }}(
    BASE_ARTIFACT_ID VARCHAR,
    GROUNDS VARCHAR,
    AUTHOR_MODEL VARCHAR,
    AGENT_REVIEW VARIANT,
    PROMPT_VERSION VARCHAR
)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Write-back: an agent-drafted STR narrative becomes FINDING n+1 + a DRAFT ARTIFACT, figures unchanged.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    unknown_artifact EXCEPTION (-20171, 'SUBMIT_AI_DRAFT: no such artifact');
    not_current EXCEPTION (-20172, 'SUBMIT_AI_DRAFT: the base artifact is not the current draft of its case');
    already_filed EXCEPTION (-20173, 'SUBMIT_AI_DRAFT: this case already has a filed STR');
    bad_narrative EXCEPTION (-20174, 'SUBMIT_AI_DRAFT: the narrative is empty or has an unfilled placeholder');
    not_approved EXCEPTION (-20175, 'SUBMIT_AI_DRAFT: the agent review did not approve this narrative');
    started TIMESTAMP_LTZ;
    run_at TIMESTAMP_NTZ;
    n INTEGER;
    v_case VARCHAR;
    base_fid VARCHAR;
    base_ver INTEGER;
    ver INTEGER;
    fid VARCHAR;
    aid VARCHAR;
    content VARIANT;
BEGIN
    started := CURRENT_TIMESTAMP();
    n := (SELECT COUNT(*) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :base_artifact_id);
    IF (n = 0) THEN
        RAISE unknown_artifact;
    END IF;
    IF (grounds IS NULL OR LENGTH(TRIM(grounds)) < 40 OR REGEXP_INSTR(grounds, '[{][a-z_]+[}]') > 0) THEN
        RAISE bad_narrative;
    END IF;
    IF (agent_review:decision::VARCHAR IS DISTINCT FROM 'APPROVE') THEN
        RAISE not_approved;
    END IF;

    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    base_fid := (SELECT MAX(finding_id) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :base_artifact_id);
    v_case := (SELECT MAX(case_id) FROM {{ EV }}.FINDING WHERE finding_id = :base_fid);
    base_ver := (SELECT MAX(version) FROM {{ EV }}.FINDING WHERE finding_id = :base_fid);

    n := (
        SELECT COUNT(*) FROM {{ EV }}.ARTIFACT a JOIN {{ EV }}.FINDING f ON f.finding_id = a.finding_id
        WHERE f.case_id = :v_case AND a.status = 'FILED'
    );
    IF (n > 0) THEN
        RAISE already_filed;
    END IF;
    -- Only the newest version may be rewritten; anything else races a redraft.
    n := (
        SELECT COUNT(*) FROM {{ EV }}.ARTIFACT
        WHERE artifact_id = :base_artifact_id AND status IN ('DRAFT', 'REVIEWED')
          AND :base_ver = (SELECT MAX(version) FROM {{ EV }}.FINDING WHERE case_id = :v_case)
    );
    IF (n = 0) THEN
        RAISE not_current;
    END IF;

    ver := base_ver + 1;
    fid := 'FND-' || LEFT(SHA2(v_case || '|' || TO_VARCHAR(ver), 256), 16);
    aid := 'STR-' || LEFT(SHA2(fid, 256), 16);

    INSERT INTO {{ EV }}.FINDING (
        finding_id, case_id, version, narrative, conclusion, cited_clause_ids, cited_evidence_ids,
        computed_values, author_model, prompt_version, agent_session_id, supersedes_finding_id
    )
    SELECT :fid, case_id, :ver, :grounds, conclusion, cited_clause_ids, cited_evidence_ids,
           computed_values, :author_model, :prompt_version, CURRENT_SESSION()::VARCHAR, :base_fid
    FROM {{ EV }}.FINDING WHERE finding_id = :base_fid;

    content := (
        SELECT OBJECT_INSERT(OBJECT_INSERT(OBJECT_INSERT(OBJECT_INSERT(OBJECT_INSERT(
                   content::OBJECT,
                   'finding_id', :fid::VARIANT, TRUE),
                   'finding_version', :ver::VARIANT, TRUE),
                   'grounds_of_suspicion', :grounds::VARIANT, TRUE),
                   'narrative_author', :author_model::VARIANT, TRUE),
                   'agent_review', :agent_review, TRUE)
        FROM {{ EV }}.ARTIFACT WHERE artifact_id = :base_artifact_id
    );

    UPDATE {{ EV }}.ARTIFACT
    SET status = 'SUPERSEDED'
    WHERE status IN ('DRAFT', 'REVIEWED')
      AND finding_id IN (SELECT finding_id FROM {{ EV }}.FINDING WHERE case_id = :v_case AND finding_id <> :fid);

    INSERT INTO {{ EV }}.ARTIFACT (
        artifact_id, finding_id, artifact_type, template_id, template_version, version, content,
        content_rendered, content_hash, status, validation_status, validation_defects
    )
    SELECT :aid, :fid, artifact_type, template_id, template_version, :ver, :content,
           TO_JSON(:content), SHA2(TO_JSON(:content), 256), 'DRAFT', 'PENDING_REVIEW', NULL
    FROM {{ EV }}.ARTIFACT WHERE artifact_id = :base_artifact_id;

    UPDATE {{ EV }}.CASE_RECORD SET status = 'DRAFTED', updated_at = :run_at WHERE case_id = :v_case;

    {{ audit("DRAFTER", T.submit_ai_draft, "OBJECT_CONSTRUCT('base_artifact_id', :base_artifact_id, 'artifact_id', :aid)", "SUCCESS",
             ":author_model", ":prompt_version", case_var="v_case") }};
    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'case_id', v_case,
        'finding_id', fid,
        'artifact_id', aid,
        'version', ver,
        'supersedes', base_artifact_id,
        'content_hash', (SELECT MAX(content_hash) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :aid)
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
