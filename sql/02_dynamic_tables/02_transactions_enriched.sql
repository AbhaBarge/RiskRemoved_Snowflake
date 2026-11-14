-- ===========================================================================
-- 02_dynamic_tables / 02_transactions_enriched.sql
-- ===========================================================================
-- Every leg, with the context detection needs: owning party, branch, account
-- type, whether the channel is cash (REF_CHANNEL, so CTR applicability is
-- reference data, not a literal), and whether either side is the bank's own
-- cash desk.
--
-- Joins are LEFT on purpose. A leg must never vanish from CURATED because a
-- reference row is missing: an unknown channel shows as is_cash NULL, a desk
-- leg as branch_region NULL -- visible, not silently filtered.
--
-- No threshold is joined here. The in-band test lives in the rule SQL, which
-- reads V_POLICY_CLAUSE_CURRENT; putting it here would mean a missing clause
-- could remove rows from the table every other consumer reads.
--
-- Carries NO label: nothing from GROUND_TRUTH is readable here.
-- ===========================================================================
{% from "02_dynamic_tables/_macros.sql" import target_lag %}

CREATE OR ALTER DYNAMIC TABLE {{ fq.curated }}.{{ dt.transactions_enriched.name }}
    TARGET_LAG = {{ target_lag(dt.transactions_enriched) }}
    WAREHOUSE = {{ wh.pipeline }}
    REFRESH_MODE = INCREMENTAL
    COMMENT = 'Leg-level ledger with party, branch, channel and cash-desk context. No label.'
AS
SELECT
    t.txn_id,
    t.txn_pair_id,
    t.leg_side,
    t.account_id,
    t.counterparty_account_id,
    t.txn_timestamp,
    TO_DATE(t.txn_timestamp)                    AS txn_date,
    t.amount,
    t.currency,
    t.amount_inr,
    t.txn_type,
    t.channel,
    ch.is_cash,
    ch.is_instant,
    t.source_ref,
    a.party_id,
    a.src_bank_id = {{ internal.bank_id }}      AS is_internal_account,
    cp.party_id                                 AS counterparty_party_id,
    cp.src_bank_id = {{ internal.bank_id }}     AS is_internal_counterparty,
    p.branch_region,
    p.branch_code,
    p.party_type,
    ra.account_type,
    -- Arrival as the app shows it: the simulator's release time if the leg was
    -- held back, otherwise when it was loaded.
    COALESCE(t.released_at, t.ingested_at)      AS ingested_at
FROM {{ fq.raw }}.RAW_TRANSACTIONS t
JOIN {{ fq.raw }}.ID_MAP_ACCOUNT a
  ON a.account_id = t.account_id
LEFT JOIN {{ fq.raw }}.ID_MAP_ACCOUNT cp
       ON cp.account_id = t.counterparty_account_id
LEFT JOIN {{ fq.curated }}.{{ dt.dim_party.name }} p
       ON p.party_id = a.party_id
LEFT JOIN {{ fq.raw }}.RAW_ACCOUNTS ra
       ON ra.account_id = t.account_id
LEFT JOIN {{ fq.governance }}.REF_CHANNEL ch
       ON ch.channel_code = t.channel
-- The live simulator: a held-back leg has not arrived, so nothing downstream --
-- signals, cases, the semantic view, the app -- can see it yet.
WHERE NOT t.held_back;
