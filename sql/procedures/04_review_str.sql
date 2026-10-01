-- ===========================================================================
-- procedures / 04_review_str.sql
-- ===========================================================================
-- REVIEW_STR(artifact_id): the Reviewer, as a procedure rather than an agent
-- mode. Its whole value is determinism (docs: "Correction to Diagram 7"), so
-- no model is involved and every check is a query:
--
--   numeric_recompute   re-run each logged query from its stored text and
--                       bind; its canonical hash must match the log, and each
--                       computed_values figure must equal the fresh result
--   citation_resolution every cited clause exists in POLICY_CLAUSE; every
--                       cited evidence id was gathered for this case
--   clause_in_effect    every cited clause had a version in force on every
--                       deposit date
--   schema              the template's required fields are present, no
--                       {placeholder} is left unfilled, and the content still
--                       matches its content_hash
--
-- REVIEW records each check, the decision, and a defect list a person can
-- read. APPROVE makes the artifact REVIEWED (attestable); REJECT makes it
-- REJECTED, and the fix is a redraft (DRAFT_STR), never an edit.
-- ===========================================================================
{% from "procedures/_macros.sql" import snapshot, audit, canonical_form with context %}
{% set T = tool %}
{% set EV = fq.evidence %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ T.review_str }}(ARTIFACT_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Reviewer: deterministic recompute, citation, clause-in-force and schema checks on one STR. No model.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    unknown_artifact EXCEPTION (-20121, 'REVIEW_STR: no such artifact');
    not_reviewable EXCEPTION (-20122, 'REVIEW_STR: only a DRAFT artifact can be reviewed');
    started TIMESTAMP_LTZ;
    run_at TIMESTAMP_NTZ;
    n INTEGER;
    v_case VARCHAR;
    v_status VARCHAR;
    fid VARCHAR;
    content VARIANT;
    stored_hash VARCHAR;
    vals VARIANT;
    cited ARRAY;
    cited_ev ARRAY;
    tmpl VARIANT;
    logs ARRAY;
    n_logs INTEGER;
    last_i INTEGER;
    q_text VARCHAR;
    p0 VARCHAR;
    ql VARCHAR;
    fresh VARIANT;
    wrapped VARCHAR;
    t0 TIMESTAMP_LTZ;
    snap VARIANT;
    ql_ms INTEGER;
    d0 DATE;
    d1 DATE;
    defects ARRAY;
    decision VARCHAR;
    rid VARCHAR;
    checks VARIANT;
BEGIN
    started := CURRENT_TIMESTAMP();
    n := (SELECT COUNT(*) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    IF (n = 0) THEN
        RAISE unknown_artifact;
    END IF;
    v_status := (SELECT MAX(status) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    IF (v_status <> 'DRAFT') THEN
        RAISE not_reviewable;
    END IF;
    fid := (SELECT MAX(finding_id) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    v_case := (SELECT MAX(case_id) FROM {{ EV }}.FINDING WHERE finding_id = :fid);
    content := (SELECT ANY_VALUE(content) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    stored_hash := (SELECT MAX(content_hash) FROM {{ EV }}.ARTIFACT WHERE artifact_id = :artifact_id);
    vals := (SELECT ANY_VALUE(computed_values) FROM {{ EV }}.FINDING WHERE finding_id = :fid);
    cited := (SELECT ANY_VALUE(cited_clause_ids) FROM {{ EV }}.FINDING WHERE finding_id = :fid);
    cited_ev := (SELECT ANY_VALUE(cited_evidence_ids) FROM {{ EV }}.FINDING WHERE finding_id = :fid);
    tmpl := (
        SELECT ANY_VALUE(t.field_definitions) FROM {{ fq.governance }}.REF_FILING_TEMPLATES t
        JOIN {{ EV }}.ARTIFACT a ON a.template_id = t.template_id AND a.template_version = t.template_version
        WHERE a.artifact_id = :artifact_id
    );

    -- 1. Recompute every query logged for the case, from the log alone.
    logs := (
        SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                   'id', q.query_log_id, 'name', q.bind_values:name::VARCHAR, 'sql', q.sql_text,
                   'p0', q.bind_values:params[0]::VARCHAR, 'hash', q.result_hash))
               WITHIN GROUP (ORDER BY q.query_log_id)
        FROM {{ EV }}.EVIDENCE_QUERY_LOG q
        JOIN {{ EV }}.EVIDENCE e ON e.evidence_id = q.evidence_id
        WHERE e.case_id = :v_case
    );
    n_logs := ARRAY_SIZE(logs);
    last_i := n_logs - 1;
    fresh := OBJECT_CONSTRUCT();
    FOR i IN 0 TO last_i DO
        ql := (SELECT GET(:logs, :i):id::VARCHAR);
        q_text := (SELECT GET(:logs, :i):sql::VARCHAR);
        p0 := (SELECT GET(:logs, :i):p0::VARCHAR);
        {{ snapshot("q_text", "p0") }}
        fresh := (SELECT OBJECT_INSERT(:fresh::OBJECT, :ql, :snap, TRUE));
    END FOR;

    -- The deposit dates, from the recomputed summary.
    d0 := (
        SELECT MAX(GET(GET(GET(:fresh, f.value:id::VARCHAR), 'result_snapshot')[0], 'FIRST_DEPOSIT_AT')::DATE)
        FROM TABLE(FLATTEN(input => :logs)) f WHERE f.value:name::VARCHAR = 'deposit_summary'
    );
    d1 := (
        SELECT MAX(GET(GET(GET(:fresh, f.value:id::VARCHAR), 'result_snapshot')[0], 'LAST_DEPOSIT_AT')::DATE)
        FROM TABLE(FLATTEN(input => :logs)) f WHERE f.value:name::VARCHAR = 'deposit_summary'
    );

    -- 2. Every defect, one row each: (check, message).
    defects := (
        SELECT COALESCE(ARRAY_AGG(OBJECT_CONSTRUCT('check', chk, 'message', msg)) WITHIN GROUP (ORDER BY chk, msg), ARRAY_CONSTRUCT())
        FROM (
            -- numeric_recompute: a logged result that no longer reproduces
            SELECT 'numeric_recompute' AS chk,
                   'Query ' || f.value:id::VARCHAR || ' (' || f.value:name::VARCHAR || ') no longer returns its logged result' AS msg
            FROM TABLE(FLATTEN(input => :logs)) f
            WHERE GET(GET(:fresh, f.value:id::VARCHAR), 'result_hash')::VARCHAR IS DISTINCT FROM f.value:hash::VARCHAR
            UNION ALL
            -- numeric_recompute: a figure that differs from its source row
            SELECT 'numeric_recompute',
                   cv.key || ' is stated as ' || TO_JSON(cv.value:value) || ' but query '
                       || cv.value:query_log_id::VARCHAR || ' returns ' || COALESCE(TO_JSON(GET(src.row_obj, cv.value:column::VARCHAR)), 'no row')
            FROM TABLE(FLATTEN(input => :vals)) cv
            LEFT JOIN (
                SELECT k.key AS placeholder, r.value AS row_obj
                FROM TABLE(FLATTEN(input => :vals)) k,
                LATERAL FLATTEN(input => GET(GET(:fresh, k.value:query_log_id::VARCHAR), 'result_snapshot')) r
                WHERE (ARRAY_SIZE(OBJECT_KEYS(k.value:row_match)) = 0 AND r.index = 0)
                   OR (ARRAY_SIZE(OBJECT_KEYS(k.value:row_match)) > 0
                       AND GET(r.value, OBJECT_KEYS(k.value:row_match)[0]::VARCHAR)
                           = GET(k.value:row_match, OBJECT_KEYS(k.value:row_match)[0]::VARCHAR))
            ) src ON src.placeholder = cv.key
            WHERE src.row_obj IS NULL
               OR TO_JSON(GET(src.row_obj, cv.value:column::VARCHAR)) IS DISTINCT FROM TO_JSON(cv.value:value)
            UNION ALL
            -- citation_resolution: clauses
            SELECT 'citation_resolution', 'Cites clause ' || c.value::VARCHAR || ', which does not exist in POLICY_CLAUSE'
            FROM TABLE(FLATTEN(input => :cited)) c
            WHERE NOT EXISTS (SELECT 1 FROM {{ fq.governance }}.POLICY_CLAUSE pc WHERE pc.clause_id = c.value::VARCHAR)
            UNION ALL
            -- citation_resolution: evidence
            SELECT 'citation_resolution', 'Cites evidence ' || c.value::VARCHAR || ', which was not gathered for this case'
            FROM TABLE(FLATTEN(input => :cited_ev)) c
            WHERE NOT EXISTS (
                SELECT 1 FROM {{ EV }}.EVIDENCE e WHERE e.evidence_id = c.value::VARCHAR AND e.case_id = :v_case
            )
            UNION ALL
            -- clause_in_effect: exists, but no version covers every deposit date
            SELECT 'clause_in_effect', 'Clause ' || c.value::VARCHAR || ' was not in force on every deposit date (' || :d0 || ' to ' || :d1 || ')'
            FROM TABLE(FLATTEN(input => :cited)) c
            WHERE EXISTS (SELECT 1 FROM {{ fq.governance }}.POLICY_CLAUSE pc WHERE pc.clause_id = c.value::VARCHAR)
              AND NOT EXISTS (
                SELECT 1 FROM {{ fq.governance }}.POLICY_CLAUSE pc
                WHERE pc.clause_id = c.value::VARCHAR
                  AND pc.effective_from <= :d0
                  AND (pc.effective_to IS NULL OR pc.effective_to > :d1)
            )
            UNION ALL
            -- schema: required fields
            SELECT 'schema', 'Required field ' || f.value::VARCHAR || ' is missing'
            FROM TABLE(FLATTEN(input => GET(:tmpl, 'required_fields'))) f
            WHERE GET(:content, f.value::VARCHAR) IS NULL
            UNION ALL
            -- schema: unfilled placeholder
            SELECT 'schema', 'The grounds of suspicion contain an unfilled placeholder'
            WHERE REGEXP_INSTR(GET(:content, 'grounds_of_suspicion')::VARCHAR, '[{][a-z_]+[}]') > 0
            UNION ALL
            -- schema: integrity of the stored artifact
            SELECT 'schema', 'The artifact content no longer matches its content_hash'
            WHERE SHA2(TO_JSON(:content), 256) <> :stored_hash
        )
    );
    decision := IFF(ARRAY_SIZE(defects) = 0, 'APPROVE', 'REJECT');
    checks := (
        SELECT OBJECT_CONSTRUCT(
            'canonical_form', '{{ canonical_form }}', 'content_hash', :stored_hash,
            'queries_recomputed', :n_logs,
            'figures_checked', ARRAY_SIZE(OBJECT_KEYS(:vals)),
            'clauses_checked', ARRAY_SIZE(:cited),
            'evidence_checked', ARRAY_SIZE(:cited_ev),
            'deposit_span', ARRAY_CONSTRUCT(:d0, :d1),
            'defects_by_check', (
                SELECT OBJECT_AGG(chk, n::VARIANT) FROM (
                    SELECT d.value:check::VARCHAR AS chk, COUNT(*) AS n
                    FROM TABLE(FLATTEN(input => :defects)) d GROUP BY 1
                )
            )
        )
    );

    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    n := (SELECT COUNT(*) FROM {{ EV }}.REVIEW r WHERE r.finding_id = :fid);
    rid := 'REV-' || LEFT(SHA2(artifact_id || '|' || TO_VARCHAR(n + 1), 256), 16);
    INSERT INTO {{ EV }}.REVIEW (
        review_id, finding_id, reviewer_type, decision, defects_found, checks_run,
        numeric_recompute_passed, citation_resolution_passed, clause_in_effect_passed, reviewer_model
    )
    SELECT :rid, :fid, 'DETERMINISTIC_PROCEDURE', :decision, :defects, :checks,
           NOT ARRAY_CONTAINS('numeric_recompute'::VARIANT, (SELECT COALESCE(ARRAY_AGG(DISTINCT d.value:check), ARRAY_CONSTRUCT()) FROM TABLE(FLATTEN(input => :defects)) d)),
           NOT ARRAY_CONTAINS('citation_resolution'::VARIANT, (SELECT COALESCE(ARRAY_AGG(DISTINCT d.value:check), ARRAY_CONSTRUCT()) FROM TABLE(FLATTEN(input => :defects)) d)),
           NOT ARRAY_CONTAINS('clause_in_effect'::VARIANT, (SELECT COALESCE(ARRAY_AGG(DISTINCT d.value:check), ARRAY_CONSTRUCT()) FROM TABLE(FLATTEN(input => :defects)) d)),
           '{{ T.review_str }} (no model)';

    UPDATE {{ EV }}.ARTIFACT
    SET status = IFF(:decision = 'APPROVE', 'REVIEWED', 'REJECTED'),
        validation_status = IFF(:decision = 'APPROVE', 'PASS', 'FAIL'),
        validation_defects = IFF(:decision = 'APPROVE', NULL, :defects::VARIANT)
    WHERE artifact_id = :artifact_id;

    UPDATE {{ EV }}.CASE_RECORD
    SET status = IFF(:decision = 'APPROVE', 'REVIEWED', 'DRAFTED'), updated_at = :run_at
    WHERE case_id = :v_case;

    {{ audit("REVIEWER", T.review_str, "OBJECT_CONSTRUCT('artifact_id', :artifact_id, 'decision', :decision, 'defects', ARRAY_SIZE(:defects))", "SUCCESS",
             "'NONE_DETERMINISTIC'", "'n/a'", case_var="v_case") }};
    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'artifact_id', artifact_id,
        'review_id', rid,
        'decision', decision,
        'defects', defects,
        'checks', checks
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
