-- ===========================================================================
-- procedures / 03_draft_str.sql
-- ===========================================================================
-- DRAFT_STR(case_id, plant_defect): the Drafter's tool. Writes a new FINDING
-- version and its STR ARTIFACT, and moves the case to DRAFTED.
--
-- Every figure is a computed_values entry: {value, display, query_log_id,
-- column, row_match}, read from an EVIDENCE_QUERY_LOG snapshot. The narrative
-- is the filing template's text with {placeholders}; it embeds no number of
-- its own. author_model records who wrote the prose (TEMPLATE_V1 while this
-- account has no AI_COMPLETE).
--
-- plant_defect = TRUE is the demo's deliberate mistake, so the Reviewer has
-- something real to catch: it inflates the deposit total by 10% and cites a
-- clause that does not exist. Both are the kind of error a language model
-- makes; REVIEW_STR finds both without one.
--
-- A redraft never overwrites: it is version n+1, supersedes version n, and the
-- older artifact becomes SUPERSEDED, so the Reviewer's catch stays on record.
-- A case whose STR was filed cannot be redrafted.
-- ===========================================================================
{% from "procedures/_macros.sql" import audit with context %}
{% set T = tool %}
{% set EV = fq.evidence %}
{% set ctr = rule["STRUCTURING_CROSS_ACCOUNT"].clause.ctr %}

{#- One computed_values entry from snapshot row `src` (an alias below). -#}
{% macro cv(src, column, log, kind, match="OBJECT_CONSTRUCT()") -%}
{% if kind == "inr" %}{% set display = EV ~ "." ~ T.format_inr ~ "(GET(" ~ src ~ ".r, '" ~ column ~ "')::FLOAT)" %}
{% elif kind == "date" %}{% set display = "TO_VARCHAR(GET(" ~ src ~ ".r, '" ~ column ~ "')::TIMESTAMP_NTZ, 'DD Mon YYYY')" %}
{% else %}{% set display = "GET(" ~ src ~ ".r, '" ~ column ~ "')::VARCHAR" %}{% endif %}
OBJECT_CONSTRUCT('value', GET({{ src }}.r, '{{ column }}'), 'display', {{ display }}, 'query_log_id', :{{ log }}, 'column', '{{ column }}', 'row_match', {{ match }})
{%- endmacro %}

{#- The log GATHER_EVIDENCE wrote for this case under `name`. -#}
{% macro log_id(name) -%}
(SELECT MAX(q.query_log_id) FROM {{ EV }}.EVIDENCE_QUERY_LOG q JOIN {{ EV }}.EVIDENCE e ON e.evidence_id = q.evidence_id WHERE e.case_id = :case_id AND q.bind_values:name::VARCHAR = '{{ name }}')
{%- endmacro %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ T.draft_str }}(CASE_ID VARCHAR, PLANT_DEFECT BOOLEAN DEFAULT FALSE)
    COPY GRANTS
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Drafter tool: new FINDING version + STR ARTIFACT from logged evidence. TRUE plants a defect for the Reviewer.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    already_filed EXCEPTION (-20111, 'DRAFT_STR: this case already has a filed STR');
    no_template EXCEPTION (-20112, 'DRAFT_STR: no active filing template');
    no_evidence EXCEPTION (-20113, 'DRAFT_STR: the evidence logs for this case are incomplete');
    started TIMESTAMP_LTZ;
    run_at TIMESTAMP_NTZ;
    n INTEGER;
    tmpl VARIANT;
    tmpl_version VARCHAR;
    prompt_ver VARCHAR;
    ql_summary VARCHAR;
    ql_deposits VARCHAR;
    ql_clauses VARCHAR;
    ql_party VARCHAR;
    vals VARIANT;
    wrong_total NUMBER(20,2);
    cited ARRAY;
    ver INTEGER;
    prev_fid VARCHAR;
    fid VARCHAR;
    aid VARCHAR;
    est TIMESTAMP_NTZ;
    sla_days INTEGER;
    sla DATE;
    content VARIANT;
    missing ARRAY;
BEGIN
    started := CURRENT_TIMESTAMP();
    -- Idempotent: a no-op when the evidence is already gathered.
    CALL {{ EV }}.{{ T.gather_evidence }}(:case_id);

    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    n := (
        SELECT COUNT(*) FROM {{ EV }}.ARTIFACT a JOIN {{ EV }}.FINDING f ON f.finding_id = a.finding_id
        WHERE f.case_id = :case_id AND a.status = 'FILED'
    );
    IF (n > 0) THEN
        RAISE already_filed;
    END IF;

    n := (SELECT COUNT(*) FROM {{ fq.governance }}.REF_FILING_TEMPLATES WHERE template_id = '{{ T.str_template_id }}' AND is_active);
    IF (n = 0) THEN
        RAISE no_template;
    END IF;
    tmpl_version := (SELECT MAX(template_version) FROM {{ fq.governance }}.REF_FILING_TEMPLATES WHERE template_id = '{{ T.str_template_id }}' AND is_active);
    tmpl := (SELECT MAX(field_definitions) FROM {{ fq.governance }}.REF_FILING_TEMPLATES WHERE template_id = '{{ T.str_template_id }}' AND template_version = :tmpl_version);
    prompt_ver := '{{ T.str_template_id }}@' || tmpl_version;

    ql_summary := {{ log_id("deposit_summary") }};
    ql_deposits := {{ log_id("deposits") }};
    ql_clauses := {{ log_id("clauses_in_force") }};
    ql_party := {{ log_id("party_profile") }};
    IF (ql_summary IS NULL OR ql_deposits IS NULL OR ql_clauses IS NULL OR ql_party IS NULL) THEN
        RAISE no_evidence;
    END IF;

    vals := (
        SELECT OBJECT_CONSTRUCT(
            'deposit_count', {{ cv("s", "DEPOSIT_COUNT", "ql_summary", "count") }},
            'deposit_total_inr', {{ cv("s", "DEPOSIT_TOTAL_INR", "ql_summary", "inr") }},
            'min_deposit_inr', {{ cv("s", "MIN_DEPOSIT_INR", "ql_summary", "inr") }},
            'max_deposit_inr', {{ cv("s", "MAX_DEPOSIT_INR", "ql_summary", "inr") }},
            'account_count', {{ cv("s", "ACCOUNT_COUNT", "ql_summary", "count") }},
            'first_deposit_at', {{ cv("s", "FIRST_DEPOSIT_AT", "ql_summary", "date") }},
            'last_deposit_at', {{ cv("s", "LAST_DEPOSIT_AT", "ql_summary", "date") }},
            'party_id', {{ cv("p", "PARTY_ID", "ql_party", "text") }},
            'party_name', {{ cv("p", "FULL_NAME", "ql_party", "text") }},
            'occupation', {{ cv("p", "OCCUPATION", "ql_party", "text") }},
            'branch_region', {{ cv("p", "BRANCH_REGION", "ql_party", "text") }},
            'declared_annual_income_inr', {{ cv("p", "DECLARED_ANNUAL_INCOME", "ql_party", "inr") }},
            'ctr_threshold_inr', {{ cv("c", "THRESHOLD_LOWER", "ql_clauses", "inr", "OBJECT_CONSTRUCT('CLAUSE_ID', " ~ ctr ~ ")") }}
        )
        FROM (SELECT result_snapshot[0] AS r FROM {{ EV }}.EVIDENCE_QUERY_LOG WHERE query_log_id = :ql_summary) s
        CROSS JOIN (SELECT result_snapshot[0] AS r FROM {{ EV }}.EVIDENCE_QUERY_LOG WHERE query_log_id = :ql_party) p
        LEFT JOIN (
            SELECT f.value AS r
            FROM {{ EV }}.EVIDENCE_QUERY_LOG q, LATERAL FLATTEN(input => q.result_snapshot) f
            WHERE q.query_log_id = :ql_clauses AND f.value:CLAUSE_ID::VARCHAR = {{ ctr }}
        ) c ON TRUE
    );
    cited := (
        SELECT ARRAY_AGG(f.value:CLAUSE_ID::VARCHAR) WITHIN GROUP (ORDER BY f.value:CLAUSE_ID::VARCHAR)
        FROM {{ EV }}.EVIDENCE_QUERY_LOG q, LATERAL FLATTEN(input => q.result_snapshot) f
        WHERE q.query_log_id = :ql_clauses
    );

    IF (plant_defect) THEN
        wrong_total := (SELECT ROUND(GET(GET(:vals, 'deposit_total_inr'), 'value')::NUMBER(20,2) * 1.1, 2));
        vals := (
            SELECT OBJECT_INSERT(:vals::OBJECT, 'deposit_total_inr',
                OBJECT_INSERT(OBJECT_INSERT(GET(:vals, 'deposit_total_inr')::OBJECT,
                    'value', :wrong_total::VARIANT, TRUE),
                    'display', {{ EV }}.{{ T.format_inr }}(:wrong_total::FLOAT)::VARIANT, TRUE),
                TRUE)
        );
        cited := (SELECT ARRAY_APPEND(:cited, {{ ctr }} || '_2026'));
    END IF;

    ver := (SELECT COALESCE(MAX(version), 0) + 1 FROM {{ EV }}.FINDING WHERE case_id = :case_id);
    prev_fid := (SELECT MAX(finding_id) FROM {{ EV }}.FINDING WHERE case_id = :case_id AND version = :ver - 1);
    fid := 'FND-' || LEFT(SHA2(case_id || '|' || TO_VARCHAR(ver), 256), 16);
    aid := 'STR-' || LEFT(SHA2(fid, 256), 16);

    INSERT INTO {{ EV }}.FINDING (
        finding_id, case_id, version, narrative, conclusion, cited_clause_ids, cited_evidence_ids,
        computed_values, author_model, prompt_version, agent_session_id, supersedes_finding_id
    )
    SELECT :fid, :case_id, :ver, GET(:tmpl, 'narrative_template')::VARCHAR, 'SUSPICIOUS', :cited,
           (SELECT ARRAY_AGG(evidence_id) WITHIN GROUP (ORDER BY evidence_id) FROM {{ EV }}.EVIDENCE WHERE case_id = :case_id),
           :vals, '{{ T.narrative_author }}', :prompt_ver, CURRENT_SESSION()::VARCHAR, :prev_fid;

    UPDATE {{ EV }}.ARTIFACT
    SET status = 'SUPERSEDED'
    -- A REJECTED draft keeps its status: the Reviewer's catch stays visible.
    WHERE status IN ('DRAFT', 'REVIEWED')
      AND finding_id IN (SELECT finding_id FROM {{ EV }}.FINDING WHERE case_id = :case_id AND finding_id <> :fid);

    -- The filing clock: established suspicion + the governed working-day SLA.
    est := (SELECT COALESCE(MAX(suspicion_established_at), :run_at) FROM {{ EV }}.CASE_RECORD WHERE case_id = :case_id);
    sla_days := (SELECT MAX(threshold_lower)::INTEGER FROM {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT WHERE clause_id = '{{ T.sla_clause_id }}');
    sla := (
        SELECT MAX(d) FROM (
            SELECT d FROM (
                SELECT DATEADD('day', ROW_NUMBER() OVER (ORDER BY SEQ4()), :est::DATE) AS d
                FROM TABLE(GENERATOR(ROWCOUNT => 60))
            )
            WHERE DAYOFWEEKISO(d) <= 5
            QUALIFY ROW_NUMBER() OVER (ORDER BY d) = :sla_days
        )
    );

    content := (
        SELECT OBJECT_CONSTRUCT(
            'report_type', 'STR', 'template', :prompt_ver,
            'case_id', :case_id,
            'finding_id', :fid,
            'finding_version', :ver,
            'subject', (SELECT result_snapshot[0] FROM {{ EV }}.EVIDENCE_QUERY_LOG WHERE query_log_id = :ql_party),
            'transactions', (SELECT result_snapshot FROM {{ EV }}.EVIDENCE_QUERY_LOG WHERE query_log_id = :ql_deposits),
            'typology_id', (
                SELECT MAX(rd.typology_id)
                FROM {{ EV }}.CASE_SIGNAL cs
                JOIN {{ EV }}.SIGNAL s ON s.signal_id = cs.signal_id
                JOIN {{ fq.governance }}.RULE_DEFINITION rd ON rd.rule_id = s.rule_id AND rd.rule_version = s.rule_version
                WHERE cs.case_id = :case_id
            ),
            'grounds_of_suspicion', {{ EV }}.{{ T.render_template }}(GET(:tmpl, 'narrative_template')::VARCHAR, :vals),
            'computed_values', :vals,
            'cited_clause_ids', :cited,
            'suspicion_established_at', :est,
            'filing_deadline', :sla,
            'narrative_author', '{{ T.narrative_author }}'
        )
    );
    missing := (
        SELECT ARRAY_AGG(f.value::VARCHAR)
        FROM TABLE(FLATTEN(input => GET(:tmpl, 'required_fields'))) f
        WHERE GET(:content, f.value::VARCHAR) IS NULL
    );

    INSERT INTO {{ EV }}.ARTIFACT (
        artifact_id, finding_id, artifact_type, template_id, template_version, version, content,
        content_rendered, content_hash, status, validation_status, validation_defects
    )
    SELECT :aid, :fid, 'STR', '{{ T.str_template_id }}', :tmpl_version, :ver, :content,
           TO_JSON(:content), SHA2(TO_JSON(:content), 256), 'DRAFT',
           IFF(ARRAY_SIZE(:missing) = 0, 'PENDING_REVIEW', 'FAIL'),
           IFF(ARRAY_SIZE(:missing) = 0, NULL, OBJECT_CONSTRUCT('missing_fields', :missing));

    UPDATE {{ EV }}.CASE_RECORD
    SET status = 'DRAFTED', suspicion_established_at = :est, sla_deadline = :sla::TIMESTAMP_NTZ,
        updated_at = :run_at
    WHERE case_id = :case_id;

    {{ audit("DRAFTER", T.draft_str, "OBJECT_CONSTRUCT('case_id', :case_id, 'plant_defect', :plant_defect)", "SUCCESS",
             "'" ~ T.narrative_author ~ "'", ":prompt_ver") }};
    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'case_id', case_id,
        'finding_id', fid,
        'artifact_id', aid,
        'version', ver,
        'planted_defect', plant_defect,
        'filing_deadline', sla
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
