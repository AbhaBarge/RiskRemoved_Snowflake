-- ===========================================================================
-- 12_investigator / 01_case_context.sql
-- ===========================================================================
-- GET_CASE_CONTEXT: the investigator agent's read-only case lookup.
--
-- Cortex Analyst only sees the question it is sent, so a follow-up such as
-- "how much did that customer deposit in the same window?" must arrive with
-- the party_id and the dates spelled out. This procedure is where the agent
-- gets them: one case, its party, its signals with their observation windows,
-- the date range of the evidence transactions, and the latest STR status.
--
-- Read-only on purpose. COLLECT_CASE_EVIDENCE (08_agent) inserts EVIDENCE rows
-- and GATHER_EVIDENCE logs queries; a lookup the agent may call on every turn
-- must not write. No name, PAN, Aadhaar or phone is returned: identifiers and
-- figures only.
-- ===========================================================================
{% set EV = fq.evidence %}
{% set ENR = fq.curated ~ "." ~ dt.transactions_enriched.name %}

CREATE OR REPLACE PROCEDURE {{ EV }}.{{ cortex.investigator_context_proc }}(CASE_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Investigator tool: read-only case context (party, signals, windows, evidence dates, STR status). No PII.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    cid VARCHAR;
    found INTEGER;
    result VARIANT;
BEGIN
    -- Ids are 'CASE-' + lowercase hex; accept any case and stray whitespace.
    cid := TRIM(:case_id);
    IF (UPPER(LEFT(:cid, 5)) = 'CASE-') THEN
        cid := 'CASE-' || LOWER(SUBSTR(:cid, 6));
    END IF;

    found := (SELECT COUNT(*) FROM {{ EV }}.CASE_RECORD WHERE case_id = :cid);
    IF (found = 0) THEN
        RETURN OBJECT_CONSTRUCT('status', 'NOT_FOUND', 'case_id', :cid,
                                'message', 'No case with this id. Ask the user to check it.');
    END IF;

    result := (
        WITH sig AS (
            SELECT s.signal_id, s.rule_id, s.rule_version, s.severity, s.metric_value,
                   s.threshold_value, s.evidence_txn_ids, s.detail, s.fired_at,
                   TRY_TO_DATE(s.window_bucket) AS window_start,
                   DATEADD('day', COALESCE(s.detail:window_days::INT, 1) - 1,
                           TRY_TO_DATE(s.window_bucket)) AS window_end
            FROM {{ EV }}.CASE_SIGNAL cs
            JOIN {{ EV }}.SIGNAL s ON s.signal_id = cs.signal_id
            WHERE cs.case_id = :cid
        ),
        txn AS (
            SELECT sig.signal_id, MIN(e.txn_date) AS first_txn_date, MAX(e.txn_date) AS last_txn_date
            FROM sig, LATERAL FLATTEN(input => sig.evidence_txn_ids) f, {{ ENR }} e
            WHERE e.txn_id = f.value::VARCHAR
            GROUP BY sig.signal_id
        ),
        str AS (
            SELECT a.artifact_id, a.status, a.version, a.created_at
            FROM {{ EV }}.ARTIFACT a
            JOIN {{ EV }}.FINDING fd ON fd.finding_id = a.finding_id
            WHERE fd.case_id = :cid
            QUALIFY ROW_NUMBER() OVER (ORDER BY a.created_at DESC, a.version DESC) = 1
        )
        SELECT OBJECT_CONSTRUCT(
            'status', 'FOUND', 'case', OBJECT_CONSTRUCT(
                'case_id', c.case_id,
                'party_id', c.party_id,
                'case_type', c.case_type,
                'case_status', c.status,
                'priority', c.priority,
                'branch_region', c.branch_region,
                'opened_at', c.opened_at::VARCHAR,
                'sla_deadline', c.sla_deadline::VARCHAR,
                'closed_at', c.closed_at::VARCHAR
            ),
            'signals', (
                SELECT ARRAY_AGG(OBJECT_CONSTRUCT(
                    'signal_id', sig.signal_id,
                    'rule_id', sig.rule_id,
                    'rule_version', sig.rule_version,
                    'severity', sig.severity,
                    'window_start', sig.window_start::VARCHAR,
                    'window_end', sig.window_end::VARCHAR,
                    'metric_value', sig.metric_value,
                    'threshold_value', sig.threshold_value,
                    'evidence_txn_count', ARRAY_SIZE(sig.evidence_txn_ids),
                    'first_txn_date', txn.first_txn_date::VARCHAR,
                    'last_txn_date', txn.last_txn_date::VARCHAR,
                    'detail', sig.detail
                )) WITHIN GROUP (ORDER BY sig.window_start, sig.signal_id)
                FROM sig LEFT JOIN txn ON txn.signal_id = sig.signal_id
            ),
            'evidence_date_range', (
                SELECT OBJECT_CONSTRUCT('first_txn_date', MIN(first_txn_date)::VARCHAR,
                                        'last_txn_date', MAX(last_txn_date)::VARCHAR)
                FROM txn
            ),
            'latest_str', (
                SELECT OBJECT_CONSTRUCT('artifact_id', artifact_id, 'str_status', status,
                                        'version', version, 'created_at', created_at::VARCHAR)
                FROM str
            ),
            'analyst_scope_hint', 'To ask about this case, filter on party_id = ' || c.party_id
                || ' (or case_id = ' || c.case_id || ') and the signal window dates.'
        )
        FROM {{ EV }}.CASE_RECORD c
        WHERE c.case_id = :cid
    );
    RETURN result;
END;
$$;
