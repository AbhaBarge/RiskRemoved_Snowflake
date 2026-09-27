-- ===========================================================================
-- 02_dynamic_tables / 06_views.sql
-- ===========================================================================
-- Pair-level view over the leg-level ledger (GAP-03): one row per transfer,
-- payer and payee side by side, each with its own amount and currency.
--
-- V_TXN_VELOCITY_FEATURES is NOT built yet: its rolling windows need lengths
-- that no config declares, and inventing them here would be a hardcoded value.
-- It lands with the velocity rule, from that rule's clauses.
-- ===========================================================================

CREATE OR REPLACE VIEW {{ fq.curated }}.{{ view.transaction_pairs }}
    COMMENT = 'One row per transfer: debit and credit legs of each txn_pair_id side by side.'
AS
SELECT
    d.txn_pair_id,
    d.txn_timestamp,
    d.account_id                AS payer_account_id,
    d.party_id                  AS payer_party_id,
    c.account_id                AS payee_account_id,
    c.party_id                  AS payee_party_id,
    -d.amount                   AS amount_paid,
    d.currency                  AS payment_currency,
    c.amount                    AS amount_received,
    c.currency                  AS receiving_currency,
    c.amount_inr                AS amount_inr,
    c.txn_type,
    c.channel,
    c.is_cash,
    d.is_internal_account       AS is_cash_desk_payer,
    d.source_ref
FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }} d
JOIN {{ fq.curated }}.{{ dt.transactions_enriched.name }} c
  ON c.txn_pair_id = d.txn_pair_id
 AND c.leg_side = 'C'
WHERE d.leg_side = 'D';
