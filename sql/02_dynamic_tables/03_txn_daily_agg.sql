-- ===========================================================================
-- 02_dynamic_tables / 03_txn_daily_agg.sql
-- ===========================================================================
-- Per customer account per day: the incremental-safe velocity base (GAP-09/10).
-- Rolling windows are summed from these daily buckets in a view rather than
-- recomputed per row in a full-refresh table.
--
-- account_id, txn_date and credit_amount_inr are the contract conf/ml.yml
-- trains velocity_anomaly on (series, timestamp, target).
--
-- The cash desk is excluded: it is the bank's till, and its "velocity" is every
-- cash customer's combined.
--
-- Lag {{ dt.txn_daily_agg.target_lag }}, not DOWNSTREAM: its consumer is a view,
-- and a DOWNSTREAM table read only by a view never refreshes (P3).
-- ===========================================================================
{% from "02_dynamic_tables/_macros.sql" import target_lag %}

CREATE OR ALTER DYNAMIC TABLE {{ fq.curated }}.{{ dt.txn_daily_agg.name }}
    TARGET_LAG = {{ target_lag(dt.txn_daily_agg) }}
    WAREHOUSE = {{ wh.pipeline }}
    REFRESH_MODE = INCREMENTAL
    COMMENT = 'Customer account x day: credit/debit counts and INR totals, cash split out. Velocity base.'
AS
SELECT
    account_id,
    party_id,
    txn_date,
    COUNT_IF(leg_side = 'C')                                    AS credit_count,
    COALESCE(SUM(IFF(leg_side = 'C', amount_inr, 0)), 0)        AS credit_amount_inr,
    COUNT_IF(leg_side = 'D')                                    AS debit_count,
    COALESCE(SUM(IFF(leg_side = 'D', -amount_inr, 0)), 0)       AS debit_amount_inr,
    COUNT_IF(leg_side = 'C' AND is_cash)                        AS cash_credit_count,
    COALESCE(SUM(IFF(leg_side = 'C' AND is_cash, amount_inr, 0)), 0) AS cash_credit_amount_inr,
    COUNT(DISTINCT counterparty_account_id)                     AS distinct_counterparties
FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }}
WHERE NOT is_internal_account
GROUP BY account_id, party_id, txn_date;
