{#- ==========================================================================
    sql/verify/controls/STRUCTURING_CROSS_ACCOUNT.sql -- the rule's false-positive
    control, included by 03_detection_metrics.sql. One SELECT, no semicolon. Admin
    only: it reads labels.

    The party-months holding a GENUINE near-miss: a cash credit on a customer
    account inside STRUCTURING_BAND of the CTR threshold, on a leg whose label
    is not illicit (P6: dev holds one). The band and CTR are read from
    V_POLICY_CLAUSE_CURRENT through the rule's clause roles, as the rule reads
    them. Nothing here decides whether the rule should fire.
    ========================================================================== -#}
{% set r = rule["STRUCTURING_CROSS_ACCOUNT"] %}
SELECT DISTINCT
    e.party_id,
    TO_CHAR(e.txn_timestamp, {{ eval_grain.month_format }}) AS month_bucket,
    TRUE                                                     AS defined
FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }} e
JOIN {{ fq.ground_truth }}.TXN_LABELS l ON l.txn_id = e.txn_id
CROSS JOIN (
    SELECT
        MAX(IFF(clause_id = {{ r.clause.ctr }}, threshold_lower, NULL))  AS ctr_threshold,
        MAX(IFF(clause_id = {{ r.clause.band }}, threshold_lower, NULL)) AS band_lower,
        MAX(IFF(clause_id = {{ r.clause.band }}, threshold_upper, NULL)) AS band_upper
    FROM {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT
) c
WHERE NOT l.is_illicit
  AND e.leg_side = 'C'
  AND e.is_cash
  AND NOT e.is_internal_account
  AND e.amount_inr BETWEEN c.band_lower * c.ctr_threshold AND c.band_upper * c.ctr_threshold
