-- ===========================================================================
-- 02_dynamic_tables / 04_party_relationship.sql
-- ===========================================================================
-- The party graph: one row per edge.
--
--   OWNS_ACCOUNT   party -> account (party_b_id NULL, account_id set)
--   FUND_FLOW      payer party -> payee party, from the debit legs, with the
--                  transfer count and INR total. DERIVED from transactions,
--                  never generated, so every flow edge is backed by legs.
--   SHARED_PHONE / SHARED_DEVICE / BENEFICIAL_OWNER
--                  the generated attribute edges, with the shared value.
--
-- FUND_FLOW EXCLUDES THE CASH DESK (obligation 6, D4): the desk is the
-- counterparty of every cash deposit, so an edge through it would join every
-- cash customer into one component and make every graph rule fire on nothing.
-- Self-flows (a party's own accounts) are not relationships and are dropped.
--
-- Lag {{ dt.party_relationship.target_lag }} (D34): a real lag, because its
-- designed consumer, DT_SIGNALS_AGGREGATE, is not built yet.
-- ===========================================================================
{% from "02_dynamic_tables/_macros.sql" import target_lag %}

CREATE OR ALTER DYNAMIC TABLE {{ fq.curated }}.{{ dt.party_relationship.name }}
    TARGET_LAG = {{ target_lag(dt.party_relationship) }}
    WAREHOUSE = {{ wh.pipeline }}
    REFRESH_MODE = INCREMENTAL
    COMMENT = 'Party graph edges: OWNS_ACCOUNT, FUND_FLOW (derived, cash desk excluded) and attribute edges.'
AS
SELECT
    'OWNS_ACCOUNT'          AS relationship_type,
    party_id                AS party_a_id,
    NULL::VARCHAR           AS party_b_id,
    account_id,
    NULL::VARCHAR           AS shared_value,
    NULL::NUMBER(38,0)      AS txn_count,
    NULL::NUMBER(38,2)      AS total_amount_inr,
    NULL::TIMESTAMP_NTZ     AS first_seen,
    NULL::TIMESTAMP_NTZ     AS last_seen
FROM {{ fq.raw }}.RAW_ACCOUNTS

UNION ALL

SELECT
    'FUND_FLOW',
    party_id,
    counterparty_party_id,
    NULL::VARCHAR,
    NULL::VARCHAR,
    COUNT(*),
    SUM(-amount_inr),
    MIN(txn_timestamp),
    MAX(txn_timestamp)
FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }}
WHERE leg_side = 'D'
  AND NOT is_internal_account
  AND NOT is_internal_counterparty
  AND party_id <> counterparty_party_id
GROUP BY party_id, counterparty_party_id

UNION ALL

SELECT
    relationship_type,
    party_a_id,
    party_b_id,
    NULL::VARCHAR,
    shared_value,
    NULL::NUMBER(38,0),
    NULL::NUMBER(38,2),
    first_seen,
    last_seen
FROM {{ fq.raw }}.RAW_PARTY_RELATIONSHIP_ATTR;
