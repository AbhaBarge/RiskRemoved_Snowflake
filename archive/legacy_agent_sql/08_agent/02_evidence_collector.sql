-- ===========================================================================
-- 08_agent / 02_evidence_collector.sql
-- ===========================================================================
-- FLAW 1 FIX: The bridge between the detection pipeline and the agent.
--
-- The pipeline populates SIGNAL, CASE_RECORD, CASE_SIGNAL automatically.
-- This procedure reads them for a given case_id and assembles the JSON
-- that GENERATE_STR expects -- so the analyst says "case CASE-xxx"
-- instead of manually querying 5 tables and building JSON.
--
-- Evidence pointers (EVIDENCE rows) are inserted as side effects: they
-- record what was read and when, without copying the data.
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

CREATE OR REPLACE PROCEDURE COLLECT_CASE_EVIDENCE(P_CASE_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Assembles all evidence for a case from the pipeline tables. Returns STR-ready JSON.'
    EXECUTE AS OWNER
AS
BEGIN
    LET v_party_id VARCHAR;
    LET v_case_type VARCHAR;
    LET v_branch VARCHAR;
    LET v_now TIMESTAMP_NTZ := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;

    -- 1. Get the case. Fail clearly if it does not exist.
    SELECT party_id, case_type, branch_region
    INTO :v_party_id, :v_case_type, :v_branch
    FROM CASE_RECORD
    WHERE case_id = :P_CASE_ID;

    IF (:v_party_id IS NULL) THEN
        RETURN OBJECT_CONSTRUCT(
            'status', 'ERROR',
            'message', 'Case not found: ' || :P_CASE_ID
        );
    END IF;

    -- 2. All signals attached to this case
    LET v_signals VARIANT := (
        SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
            'signal_id', s.signal_id,
            'rule_id', s.rule_id,
            'rule_version', s.rule_version,
            'severity', s.severity,
            'metric_value', s.metric_value,
            'threshold_value', s.threshold_value,
            'evidence_txn_ids', s.evidence_txn_ids,
            'detail', s.detail,
            'fired_at', s.fired_at::VARCHAR,
            'window_bucket', s.window_bucket
        ))
        FROM SIGNAL s
        JOIN CASE_SIGNAL cs ON cs.signal_id = s.signal_id
        WHERE cs.case_id = :P_CASE_ID
    );

    -- 3. Flatten all evidence transaction IDs across signals
    LET v_all_txn_ids ARRAY := (
        SELECT ARRAY_AGG(DISTINCT t.VALUE::VARCHAR)
        FROM SIGNAL s
        JOIN CASE_SIGNAL cs ON cs.signal_id = s.signal_id,
        LATERAL FLATTEN(input => s.evidence_txn_ids) t
        WHERE cs.case_id = :P_CASE_ID
    );

    -- 4. Party details from the dynamic table
    LET v_party VARIANT := (
        SELECT OBJECT_CONSTRUCT(
            'party_id', party_id,
            'full_name', full_name,
            'party_type', party_type,
            'nationality', nationality,
            'tax_id', tax_id,
            'kyc_status', kyc_status,
            'risk_rating', risk_rating,
            'branch_region', branch_region
        )
        FROM {{ fq.curated }}.{{ dt.dim_party.name }}
        WHERE party_id = :v_party_id
    );

    -- 5. The actual transactions that caused the signals
    LET v_transactions VARIANT := (
        SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
            'txn_id', txn_id,
            'date', txn_date::VARCHAR,
            'amount', amount_inr,
            'channel', channel,
            'from', debit_party_name,
            'to', credit_party_name,
            'is_cash', is_cash,
            'is_cross_border', is_cross_border
        ) ORDER BY txn_timestamp)
        FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }}
        WHERE txn_id IN (SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(input => :v_all_txn_ids)))
    );

    -- 6. Policy clauses cited by the signals' rules
    LET v_clauses VARIANT := (
        SELECT ARRAY_AGG(DISTINCT OBJECT_CONSTRUCT(
            'clause_id', pc.clause_id,
            'document_title', pc.document_title,
            'clause_ref', pc.clause_ref,
            'metric', pc.metric,
            'operator', pc.operator,
            'threshold_lower', pc.threshold_lower,
            'threshold_upper', pc.threshold_upper,
            'action_required', pc.action_required
        ))
        FROM {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT pc
        JOIN {{ fq.governance }}.RULE_DEFINITION rd ON ARRAY_CONTAINS(pc.clause_id::VARIANT, rd.clause_ids)
        WHERE rd.rule_id IN (
            SELECT DISTINCT s.rule_id
            FROM SIGNAL s
            JOIN CASE_SIGNAL cs ON cs.signal_id = s.signal_id
            WHERE cs.case_id = :P_CASE_ID
        )
          AND rd.is_active
    );

    -- 7. Insert EVIDENCE pointer rows (not copies, just references)
    -- Party evidence
    INSERT INTO EVIDENCE (evidence_id, case_id, ref_type, ref_id, ref_table, as_of_ts, materiality, gathered_by)
    SELECT UUID_STRING(), :P_CASE_ID, 'PARTY', :v_party_id,
           '{{ fq.curated }}.{{ dt.dim_party.name }}', :v_now, 'PRIMARY', 'PROC:COLLECT_CASE_EVIDENCE';

    -- Transaction evidence (one row per transaction)
    INSERT INTO EVIDENCE (evidence_id, case_id, ref_type, ref_id, ref_table, as_of_ts, materiality, gathered_by)
    SELECT UUID_STRING(), :P_CASE_ID, 'TRANSACTION', VALUE::VARCHAR,
           '{{ fq.curated }}.{{ dt.transactions_enriched.name }}', :v_now, 'PRIMARY', 'PROC:COLLECT_CASE_EVIDENCE'
    FROM TABLE(FLATTEN(input => :v_all_txn_ids));

    -- 8. Pre-compute all numbers in SQL (Flaw 4 fix)
    LET v_total_amount NUMBER(18,2) := (
        SELECT SUM(amount_inr)
        FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }}
        WHERE txn_id IN (SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(input => :v_all_txn_ids)))
    );
    LET v_txn_count INTEGER := ARRAY_SIZE(:v_all_txn_ids);
    LET v_distinct_accounts INTEGER := (
        SELECT COUNT(DISTINCT account_id)
        FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }}
        WHERE txn_id IN (SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(input => :v_all_txn_ids)))
    );

    -- 9. Assemble the STR-ready JSON
    LET v_suspicion_codes ARRAY := ARRAY_CONSTRUCT('D', 'E', 'F');
    LET v_result VARIANT := OBJECT_CONSTRUCT(
        'status', 'SUCCESS',
        'case_id', :P_CASE_ID,
        'subject_name', :v_party:full_name,
        'subject_type', :v_party:party_type,
        'subject_id', :v_party:tax_id,
        'accounts', :v_transactions,
        'transactions', :v_transactions,
        'total_amount', :v_total_amount,
        'transaction_count', :v_txn_count,
        'suspicion_codes', :v_suspicion_codes,
        'suspicion_summary', 'Auto-assembled from case ' || :P_CASE_ID || ': '
            || ARRAY_SIZE(:v_signals)::VARCHAR || ' signal(s), '
            || :v_case_type || ' case, '
            || :v_txn_count::VARCHAR || ' evidence transactions totalling '
            || :v_total_amount::VARCHAR || ' INR.',
        'typology', :v_case_type,
        'signals', :v_signals,
        'policy_clauses_cited', :v_clauses,
        'branch_name', :v_branch,
        'computed_values', OBJECT_CONSTRUCT(
            'total_amount', :v_total_amount,
            'transaction_count', :v_txn_count,
            'distinct_accounts', :v_distinct_accounts,
            'signal_count', ARRAY_SIZE(:v_signals)
        )
    );

    RETURN :v_result;
END;
