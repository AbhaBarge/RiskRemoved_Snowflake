-- ===========================================================================
-- 03_streams_tasks / 01_route_signals.sql
-- ===========================================================================
-- DT_SIGNALS_FAST -> SIGNALS_STREAM -> ROUTE_SIGNALS_TASK -> ROUTE_SIGNALS
--   -> SIGNAL, CASE_RECORD, CASE_SIGNAL.
--
-- The dynamic table re-emits every live signal on every refresh; the router
-- MERGEs on signal_business_key, so a re-emission is harmless (GAP-11).
--
-- A stream on a dynamic table cannot be created with SHOW_INITIAL_ROWS (CREATE
-- STREAM reference), so it starts empty: rows already in the table never reach
-- it. ROUTE_SIGNALS(TRUE) reads the whole table instead (D36) -- run once after
-- the first deploy, by the replay harness, and to recover a stale stream.
--
-- Cases are opened from SIGNAL state, not from the batch: any signal at or
-- above routing.min_severity_for_case that is on no case yet. A run that
-- failed half-way therefore leaves nothing a later run cannot finish.
-- ===========================================================================
{% set stream = fq.curated ~ "." ~ stream_task.signals_stream %}
{% set proc = fq.evidence ~ "." ~ stream_task.route_signals_procedure %}
{% set task = fq.evidence ~ "." ~ stream_task.route_signals_task %}

{#- The signals to route: the stream's inserts, or the whole table on a full
    scan. The stream is read either way, so a full scan also consumes it. A
    DELETE (a signal gone from the table) is ignored: SIGNAL is evidence and
    is never deleted. -#}
{% macro batch() -%}
SELECT rule_id, party_id, account_id, window_bucket, metric_value, threshold_value,
       evidence_txn_ids, detail, last_txn_at, signal_business_key
FROM {{ stream }}
WHERE METADATA$ACTION = 'INSERT' AND NOT :full_scan
UNION ALL
SELECT rule_id, party_id, account_id, window_bucket, metric_value, threshold_value,
       evidence_txn_ids, detail, last_txn_at, signal_business_key
FROM {{ fq.curated }}.{{ dt.signals_fast.name }}
WHERE :full_scan
{%- endmacro %}

{#- The rule version and severity a signal is recorded under: the highest
    active version in the registry the policy stage governs. -#}
{% macro active_rules() -%}
SELECT rule_id, rule_version, severity
FROM {{ fq.governance }}.RULE_DEFINITION
WHERE is_active
QUALIFY ROW_NUMBER() OVER (PARTITION BY rule_id ORDER BY rule_version DESC) = 1
{%- endmacro %}

{% macro is_open(status) -%}
ARRAY_CONTAINS({{ status }}::VARIANT, {{ routing.open_case_statuses }})
{%- endmacro %}

{#- Signals owed a case: severe enough, and on no case yet. -#}
{% macro candidates() -%}
SELECT s.signal_id, s.signal_business_key, s.party_id, s.rule_id
FROM {{ fq.evidence }}.SIGNAL s
WHERE ARRAY_POSITION(s.severity::VARIANT, {{ routing.severity_order }})
      >= ARRAY_POSITION({{ routing.min_severity_for_case }}::VARIANT, {{ routing.severity_order }})
  AND NOT EXISTS (SELECT 1 FROM {{ fq.evidence }}.CASE_SIGNAL cs WHERE cs.signal_id = s.signal_id)
{%- endmacro %}

{#- A case's type over its signals `alias`: CROSS_DOMAIN once it holds enough
    distinct rules, or rules of more than one type; otherwise its rules' type. -#}
{% macro case_type(alias) -%}
IFF(COUNT(DISTINCT {{ alias }}.rule_id) >= {{ routing.cross_domain_min_distinct_rules }}
        OR COUNT(DISTINCT GET({{ routing.case_type_by_rule }}, {{ alias }}.rule_id)::VARCHAR) > 1,
    'CROSS_DOMAIN',
    MIN(GET({{ routing.case_type_by_rule }}, {{ alias }}.rule_id)::VARCHAR))
{%- endmacro %}

-- --------------------------------------------------------------------------
-- The stream. IF NOT EXISTS, never OR REPLACE: replacing a stream resets its
-- offset, so a redeploy would silently drop the changes not yet routed.
-- --------------------------------------------------------------------------
CREATE STREAM IF NOT EXISTS {{ stream }}
    ON DYNAMIC TABLE {{ fq.curated }}.{{ dt.signals_fast.name }}
    COMMENT = 'Changes to the signal table, consumed only by ROUTE_SIGNALS.';

-- --------------------------------------------------------------------------
-- The router. One transaction: a failure rolls back every write and leaves the
-- stream unconsumed, so the next run sees the same changes.
-- --------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE {{ proc }}(FULL_SCAN BOOLEAN DEFAULT FALSE)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Routes signals into SIGNAL / CASE_RECORD / CASE_SIGNAL. Idempotent on signal_business_key. TRUE reads the whole signal table.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    unknown_rule EXCEPTION (-20003, 'ROUTE_SIGNALS: a signal names a rule with no active RULE_DEFINITION row');
    unattached EXCEPTION (-20004, 'ROUTE_SIGNALS: a signal owed a case was not attached to one');
    run_at TIMESTAMP_NTZ;
    unknown_signals INTEGER;
    signals_before INTEGER;
    signals_merged INTEGER;
    signals_inserted INTEGER;
    owed INTEGER;
    cases_opened INTEGER;
    signals_attached INTEGER;
    cases_escalated INTEGER;
BEGIN
    BEGIN TRANSACTION;
    run_at := CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;

    unknown_signals := (
        SELECT COUNT(*) FROM ({{ batch() }}) b
        WHERE NOT EXISTS (SELECT 1 FROM ({{ active_rules() }}) r WHERE r.rule_id = b.rule_id)
    );
    IF (unknown_signals > 0) THEN
        RAISE unknown_rule;
    END IF;

    -- 1. SIGNAL. A re-fire refreshes the evidence (D37) and keeps signal_id and
    --    first_seen_at. A row whose evidence did not change is not touched, so
    --    re-reading an unchanged table writes nothing.
    signals_before := (SELECT COUNT(*) FROM {{ fq.evidence }}.SIGNAL);
    MERGE INTO {{ fq.evidence }}.SIGNAL t
    USING (
        SELECT
            {{ routed_id.signal_prefix }} || LEFT(SHA2(b.signal_business_key, 256), {{ routed_id.hex }}) AS signal_id,
            b.signal_business_key,
            b.rule_id,
            r.rule_version,
            b.party_id,
            b.account_id,
            r.severity,
            b.window_bucket,
            b.last_txn_at AS fired_at,
            b.metric_value,
            b.threshold_value,
            b.evidence_txn_ids,
            b.detail
        FROM ({{ batch() }}) b
        JOIN ({{ active_rules() }}) r ON r.rule_id = b.rule_id
    ) s
    ON t.signal_business_key = s.signal_business_key
    WHEN MATCHED AND (
           t.rule_version IS DISTINCT FROM s.rule_version
        OR t.severity IS DISTINCT FROM s.severity
        OR t.fired_at IS DISTINCT FROM s.fired_at
        OR t.metric_value IS DISTINCT FROM s.metric_value
        OR t.threshold_value IS DISTINCT FROM s.threshold_value
        OR t.evidence_txn_ids IS DISTINCT FROM s.evidence_txn_ids
        OR t.detail IS DISTINCT FROM s.detail
    ) THEN UPDATE SET
        rule_version = s.rule_version,
        severity = s.severity,
        fired_at = s.fired_at,
        as_of_ts = :run_at,
        metric_value = s.metric_value,
        threshold_value = s.threshold_value,
        evidence_txn_ids = s.evidence_txn_ids,
        detail = s.detail,
        last_seen_at = :run_at
    WHEN NOT MATCHED THEN INSERT (
        signal_id, signal_business_key, rule_id, rule_version, party_id, account_id,
        severity, window_bucket, fired_at, as_of_ts, metric_value, threshold_value,
        evidence_txn_ids, detail, first_seen_at, last_seen_at
    ) VALUES (
        s.signal_id, s.signal_business_key, s.rule_id, s.rule_version, s.party_id, s.account_id,
        s.severity, s.window_bucket, s.fired_at, :run_at, s.metric_value, s.threshold_value,
        s.evidence_txn_ids, s.detail, :run_at, :run_at
    );
    signals_merged := SQLROWCOUNT;
    signals_inserted := (SELECT COUNT(*) - :signals_before FROM {{ fq.evidence }}.SIGNAL);

    -- 2. One new case per party owed a case and holding no open one. Its id is
    --    derived from the party and its smallest opening key (ids.case_id).
    --    sla_deadline stays NULL: the clock runs from established suspicion,
    --    not from an alert. branch_region is NOT NULL, so a party missing from
    --    DT_DIM_PARTY fails the run rather than opening an unfilterable case.
    owed := (SELECT COUNT(*) FROM ({{ candidates() }}));
    INSERT INTO {{ fq.evidence }}.CASE_RECORD (case_id, party_id, case_type, branch_region, opened_at, updated_at)
    SELECT
        {{ routed_id.case_prefix }} || LEFT(SHA2(c.party_id || '|' || MIN(c.signal_business_key), 256), {{ routed_id.hex }}),
        c.party_id,
        {{ case_type("c") }},
        p.branch_region,
        :run_at,
        :run_at
    FROM ({{ candidates() }}) c
    LEFT JOIN {{ fq.curated }}.{{ dt.dim_party.name }} p ON p.party_id = c.party_id
    WHERE NOT EXISTS (
        SELECT 1 FROM {{ fq.evidence }}.CASE_RECORD o
        WHERE o.party_id = c.party_id AND {{ is_open("o.status") }}
    )
    GROUP BY c.party_id, p.branch_region;
    cases_opened := SQLROWCOUNT;

    -- 3. Attach every owed signal to its party's open case (the earliest, if a
    --    party somehow holds two).
    INSERT INTO {{ fq.evidence }}.CASE_SIGNAL (case_id, signal_id, attached_at)
    SELECT o.case_id, c.signal_id, :run_at
    FROM ({{ candidates() }}) c
    JOIN (
        SELECT case_id, party_id
        FROM {{ fq.evidence }}.CASE_RECORD
        WHERE {{ is_open("status") }}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY party_id ORDER BY opened_at, case_id) = 1
    ) o ON o.party_id = c.party_id;
    signals_attached := SQLROWCOUNT;
    IF (signals_attached <> owed) THEN
        RAISE unattached;
    END IF;

    -- 4. Re-type open cases whose signals now span enough rules (CROSS_DOMAIN).
    UPDATE {{ fq.evidence }}.CASE_RECORD c
    SET case_type = x.case_type, updated_at = :run_at
    FROM (
        SELECT cs.case_id, {{ case_type("s") }} AS case_type
        FROM {{ fq.evidence }}.CASE_SIGNAL cs
        JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_id = cs.signal_id
        GROUP BY cs.case_id
    ) x
    WHERE c.case_id = x.case_id
      AND {{ is_open("c.status") }}
      AND c.case_type IS DISTINCT FROM x.case_type;
    cases_escalated := SQLROWCOUNT;

    COMMIT;
    RETURN OBJECT_CONSTRUCT(
        'full_scan', full_scan,
        'signals_inserted', signals_inserted,
        'signals_updated', signals_merged - signals_inserted,
        'cases_opened', cases_opened,
        'signals_attached', signals_attached,
        'cases_escalated', cases_escalated
    );
EXCEPTION
    WHEN OTHER THEN
        ROLLBACK;
        RAISE;
END;
$$;

-- --------------------------------------------------------------------------
-- The task. Triggered, not scheduled (D38): it runs when a refresh changed a
-- signal. Suspended before CREATE OR ALTER (a resumed task cannot be altered),
-- and resumed again only where streams_tasks.resume_route_task says so. The
-- router's counts become the run's return value, visible in TASK_HISTORY.
-- --------------------------------------------------------------------------
ALTER TASK IF EXISTS {{ task }} SUSPEND;

CREATE OR ALTER TASK {{ task }}
    WAREHOUSE = {{ wh.pipeline }}
    COMMENT = 'Runs ROUTE_SIGNALS when SIGNALS_STREAM has data.'
    WHEN SYSTEM$STREAM_HAS_DATA('{{ stream }}')
AS
EXECUTE IMMEDIATE $$
BEGIN
    CALL {{ proc }}(FALSE);
    LET counts VARCHAR := (SELECT TO_JSON($1) FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
    CALL SYSTEM$SET_RETURN_VALUE(:counts);
END;
$$;
{% if stream_task.resume_route_task %}

ALTER TASK {{ task }} RESUME;
{% endif %}
