-- ===========================================================================
-- procedures / 02_gather_evidence.sql
-- ===========================================================================
-- GATHER_EVIDENCE(case_id): the Investigator's first tool. Writes
--   EVIDENCE            pointers to the signal(s), every cited transaction, the
--                       party, and each policy clause in force on the dates
--   EVIDENCE_QUERY_LOG  the four queries every later figure comes from, each
--                       with its exact text, bind values, canonical result
--                       snapshot and hash
-- and moves the case OPEN -> INVESTIGATING.
--
-- Idempotent: evidence and log ids are derived from what they describe, and a
-- log already present is not re-run, so its snapshot stays the one taken first.
-- That is the baseline REVIEW_STR recomputes against.
--
-- Every query takes exactly one bind, the case id, so it can be re-run from the
-- log alone: EXECUTE IMMEDIATE <sql_text> USING (<params[0]>).
-- ===========================================================================
{% from "procedures/_macros.sql" import snapshot, audit, canonical_form with context %}
{% set T = tool %}
{% set ENR = fq.curated ~ "." ~ dt.transactions_enriched.name %}
{% set PARTY = fq.curated ~ "." ~ dt.dim_party.name %}

{#- The deposits a case's signals cite. Shared by three of the queries. -#}
{% set deposits_cte %}
WITH p AS (SELECT ?::VARCHAR AS case_id),
ids AS (
    SELECT DISTINCT f.value::VARCHAR AS txn_id
    FROM {{ fq.evidence }}.CASE_SIGNAL cs
    JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id,
    LATERAL FLATTEN(input => s.evidence_txn_ids) f
    WHERE cs.case_id = (SELECT case_id FROM p)
),
d AS (
    SELECT t.txn_id, t.txn_timestamp, t.account_id, t.amount_inr, t.channel, t.txn_type, t.branch_code
    FROM ids JOIN {{ ENR }} t ON t.txn_id = ids.txn_id
)
{% endset %}

{% set q_summary %}
{{ deposits_cte }}SELECT COUNT(*) AS deposit_count,
       SUM(amount_inr)::NUMBER(20,2) AS deposit_total_inr,
       MIN(amount_inr)::NUMBER(20,2) AS min_deposit_inr,
       MAX(amount_inr)::NUMBER(20,2) AS max_deposit_inr,
       COUNT(DISTINCT account_id) AS account_count,
       MIN(txn_timestamp) AS first_deposit_at,
       MAX(txn_timestamp) AS last_deposit_at
FROM d
{% endset %}

{% set q_deposits %}
{{ deposits_cte }}SELECT txn_id, txn_timestamp, account_id, amount_inr::NUMBER(20,2) AS amount_inr,
       channel, txn_type, branch_code
FROM d
{% endset %}

{#- Each clause the case's rules cite, plus the filing deadline and the
    attestation duty, at the latest version in force across the whole span of
    the deposits. A clause absent here was not in force: the Reviewer's
    clause_in_effect check rests on that. -#}
{% set q_clauses %}
{{ deposits_cte }}, r AS (
    SELECT DISTINCT c.value::VARCHAR AS clause_id
    FROM {{ fq.evidence }}.CASE_SIGNAL cs
    JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id
    JOIN {{ fq.governance }}.RULE_DEFINITION rd
      ON rd.rule_id = s.rule_id AND rd.rule_version = s.rule_version,
    LATERAL FLATTEN(input => rd.clause_ids) c
    WHERE cs.case_id = (SELECT case_id FROM p)
    UNION SELECT '{{ T.sla_clause_id }}'
    UNION SELECT '{{ T.attestation_clause_id }}'
),
span AS (SELECT MIN(txn_timestamp)::DATE AS d0, MAX(txn_timestamp)::DATE AS d1 FROM d)
SELECT pc.clause_id, pc.clause_version, pc.metric, pc.operator, pc.threshold_lower,
       pc.threshold_upper, pc.action_required, pc.effective_from, pc.effective_to,
       pc.section_heading, pc.source_document
FROM {{ fq.governance }}.POLICY_CLAUSE pc
JOIN r ON r.clause_id = pc.clause_id
CROSS JOIN span
WHERE pc.effective_from <= span.d0
  AND (pc.effective_to IS NULL OR pc.effective_to > span.d1)
QUALIFY ROW_NUMBER() OVER (PARTITION BY pc.clause_id ORDER BY pc.clause_version DESC) = 1
{% endset %}

{% set q_party %}
SELECT p.party_id, p.full_name, p.party_type, p.occupation,
       p.declared_annual_income::NUMBER(20,2) AS declared_annual_income,
       p.source_of_funds, p.risk_rating, p.pep_flag, p.kyc_status,
       p.branch_region, p.branch_code, p.customer_since, p.pan_number
FROM {{ PARTY }} p
JOIN {{ fq.evidence }}.CASE_RECORD c ON c.party_id = p.party_id
WHERE c.case_id = ?
{% endset %}

{#- Log one query against evidence row `ev` unless that exact log exists. -#}
{% macro log_query(name, qvar, ev) %}
    ql_id := 'QL-' || LEFT(SHA2({{ ev }} || '|' || SHA2({{ qvar }}, 256), 256), 16);
    n := (SELECT COUNT(*) FROM {{ fq.evidence }}.EVIDENCE_QUERY_LOG WHERE query_log_id = :ql_id);
    IF (n = 0) THEN
        {{ snapshot(qvar, "case_id") }}
        INSERT INTO {{ fq.evidence }}.EVIDENCE_QUERY_LOG (
            query_log_id, evidence_id, sql_text, sql_hash, bind_values, result_columns,
            result_row_count, result_snapshot, result_hash, canonical_form_version,
            warehouse_name, executed_at, execution_ms
        )
        SELECT :ql_id, :{{ ev }}, :{{ qvar }}, SHA2(:{{ qvar }}, 256),
               OBJECT_CONSTRUCT('name', '{{ name }}', 'params', ARRAY_CONSTRUCT(:case_id)),
               GET(:snap, 'result_columns')::ARRAY, GET(:snap, 'row_count')::NUMBER,
               GET(:snap, 'result_snapshot'), GET(:snap, 'result_hash')::VARCHAR,
               '{{ canonical_form }}', CURRENT_WAREHOUSE(), :run_at, :ql_ms;
        logged := logged + 1;
    END IF;
{% endmacro %}

CREATE OR REPLACE PROCEDURE {{ fq.evidence }}.{{ T.gather_evidence }}(CASE_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Investigator tool: evidence pointers + logged, hashed queries for one case. Idempotent.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    unknown_case EXCEPTION (-20101, 'GATHER_EVIDENCE: no such case');
    no_signal EXCEPTION (-20102, 'GATHER_EVIDENCE: the case holds no signal');
    started TIMESTAMP_LTZ;
    run_at TIMESTAMP_NTZ;
    n INTEGER;
    v_party VARCHAR;
    anchor VARCHAR;
    party_ev VARCHAR;
    ev_before INTEGER;
    ev_after INTEGER;
    logged INTEGER DEFAULT 0;
    ql_id VARCHAR;
    wrapped VARCHAR;
    t0 TIMESTAMP_LTZ;
    snap VARIANT;
    ql_ms INTEGER;
    q_summary VARCHAR DEFAULT '{{ q_summary | trim | replace("'", "''") }}';
    q_deposits VARCHAR DEFAULT '{{ q_deposits | trim | replace("'", "''") }}';
    q_clauses VARCHAR DEFAULT '{{ q_clauses | trim | replace("'", "''") }}';
    q_party VARCHAR DEFAULT '{{ q_party | trim | replace("'", "''") }}';
BEGIN
    started := CURRENT_TIMESTAMP();
    n := (SELECT COUNT(*) FROM {{ fq.evidence }}.CASE_RECORD WHERE case_id = :case_id);
    IF (n = 0) THEN
        RAISE unknown_case;
    END IF;
    v_party := (SELECT MAX(party_id) FROM {{ fq.evidence }}.CASE_RECORD WHERE case_id = :case_id);
    -- The anchor: the case's first signal. Deposit, clause and summary queries
    -- hang off its evidence row.
    anchor := (
        SELECT MAX('EV-' || LEFT(SHA2(:case_id || '|SIGNAL|' || signal_id, 256), 16))
        FROM (
            SELECT s.signal_id
            FROM {{ fq.evidence }}.CASE_SIGNAL cs
            JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id
            WHERE cs.case_id = :case_id
            ORDER BY s.first_seen_at, s.signal_id
            LIMIT 1
        )
    );
    IF (anchor IS NULL) THEN
        RAISE no_signal;
    END IF;
    party_ev := 'EV-' || LEFT(SHA2(case_id || '|PARTY|' || v_party, 256), 16);

    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
    ev_before := (SELECT COUNT(*) FROM {{ fq.evidence }}.EVIDENCE WHERE case_id = :case_id);

    MERGE INTO {{ fq.evidence }}.EVIDENCE t
    USING (
        SELECT 'EV-' || LEFT(SHA2(:case_id || '|' || ref_type || '|' || ref_id, 256), 16) AS evidence_id,
               ref_type, ref_id, ref_table, materiality
        FROM (
            SELECT 'SIGNAL' AS ref_type, s.signal_id AS ref_id,
                   '{{ fq.evidence }}.SIGNAL' AS ref_table, 'PRIMARY' AS materiality
            FROM {{ fq.evidence }}.CASE_SIGNAL cs
            JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id
            WHERE cs.case_id = :case_id
            UNION
            SELECT 'TRANSACTION', f.value::VARCHAR, '{{ ENR }}', 'PRIMARY'
            FROM {{ fq.evidence }}.CASE_SIGNAL cs
            JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id,
            LATERAL FLATTEN(input => s.evidence_txn_ids) f
            WHERE cs.case_id = :case_id
            UNION
            SELECT 'PARTY', :v_party, '{{ PARTY }}', 'CONTEXT'
        )
    ) s
    ON t.evidence_id = s.evidence_id
    WHEN NOT MATCHED THEN INSERT (
        evidence_id, case_id, ref_type, ref_id, ref_table, as_of_ts, materiality, gathered_by
    ) VALUES (
        s.evidence_id, :case_id, s.ref_type, s.ref_id, s.ref_table, :run_at, s.materiality,
        '{{ T.gather_evidence }}'
    );
{{ log_query("deposit_summary", "q_summary", "anchor") }}
{{ log_query("deposits", "q_deposits", "anchor") }}
{{ log_query("clauses_in_force", "q_clauses", "anchor") }}
{{ log_query("party_profile", "q_party", "party_ev") }}
    -- One pointer per clause the logged query found in force.
    MERGE INTO {{ fq.evidence }}.EVIDENCE t
    USING (
        SELECT 'EV-' || LEFT(SHA2(:case_id || '|POLICY_CLAUSE|' || ref_id, 256), 16) AS evidence_id, ref_id
        FROM (
            SELECT f.value:CLAUSE_ID::VARCHAR || '@v' || f.value:CLAUSE_VERSION::VARCHAR AS ref_id
            FROM {{ fq.evidence }}.EVIDENCE_QUERY_LOG q,
            LATERAL FLATTEN(input => q.result_snapshot) f
            WHERE q.evidence_id = :anchor AND q.bind_values:name::VARCHAR = 'clauses_in_force'
        )
    ) s
    ON t.evidence_id = s.evidence_id
    WHEN NOT MATCHED THEN INSERT (
        evidence_id, case_id, ref_type, ref_id, ref_table, as_of_ts, materiality, gathered_by
    ) VALUES (
        s.evidence_id, :case_id, 'POLICY_CLAUSE', s.ref_id, '{{ fq.governance }}.POLICY_CLAUSE',
        :run_at, 'SUPPORTING', '{{ T.gather_evidence }}'
    );
    ev_after := (SELECT COUNT(*) FROM {{ fq.evidence }}.EVIDENCE WHERE case_id = :case_id);

    UPDATE {{ fq.evidence }}.CASE_RECORD
    SET status = 'INVESTIGATING', updated_at = :run_at
    WHERE case_id = :case_id AND status = 'OPEN';

    {{ audit("INVESTIGATOR", T.gather_evidence, "OBJECT_CONSTRUCT('case_id', :case_id)", "SUCCESS",
             "'NONE_DETERMINISTIC'", "'n/a'",
             evidence="(SELECT ARRAY_AGG(evidence_id) WITHIN GROUP (ORDER BY evidence_id) FROM " ~ fq.evidence ~ ".EVIDENCE WHERE case_id = :case_id)") }};
    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'case_id', case_id,
        'evidence_rows', ev_after,
        'evidence_inserted', ev_after - ev_before,
        'query_logs_inserted', logged
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;
