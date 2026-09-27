{#- ==========================================================================
    sql/rules/STRUCTURING_CROSS_ACCOUNT.sql -- one SELECT, included by the
    signal dynamic table named in conf/detection.yml (rules[].dynamic_table).

    RULE_DEFINITION.rule_sql_hash is the SHA-256 of THIS FILE's source. Editing
    it changes the hash, and the policy stage then refuses to deploy until the
    rule is given a new rule_version: a changed detection is a new rule.

    No semicolon, no threshold, no clause id literal: every number is read from
    V_POLICY_CLAUSE_CURRENT at refresh time, through the clause ids config
    assigns to this rule's roles.
    ========================================================================== -#}
{% set r = rule["STRUCTURING_CROSS_ACCOUNT"] %}
-- STRUCTURING_CROSS_ACCOUNT: one party, cash deposits within STRUCTURING_BAND of
-- the CTR threshold, into at least STRUCTURING_MIN_ACCOUNTS distinct accounts,
-- within one tumbling STRUCTURING_WINDOW_DAYS window counted from the anchor.
-- A deposit is a cash credit leg on a customer account (the desk is the bank).
SELECT
    {{ r.id }}                                                      AS rule_id,
    d.party_id,
    NULL::VARCHAR                                                   AS account_id,
    TO_VARCHAR(DATEADD('day', (d.bucket * d.window_days)::INT, {{ r.anchor }})) AS window_bucket,
    DATEADD('day', (d.bucket * d.window_days)::INT, {{ r.anchor }}) AS window_start,
    DATEADD('day', ((d.bucket + 1) * d.window_days)::INT - 1, {{ r.anchor }}) AS window_end,
    COUNT(DISTINCT d.account_id)::NUMBER(20,4)                      AS metric_value,
    d.min_accounts::NUMBER(20,4)                                    AS threshold_value,
    ARRAY_AGG(d.txn_id) WITHIN GROUP (ORDER BY d.txn_id)            AS evidence_txn_ids,
    OBJECT_CONSTRUCT(
        'deposit_count', COUNT(*),
        'deposit_total_inr', SUM(d.amount_inr),
        'accounts', ARRAY_AGG(DISTINCT d.account_id) WITHIN GROUP (ORDER BY d.account_id),
        'band_lower_inr', d.band_lower * d.ctr_threshold,
        'band_upper_inr', d.band_upper * d.ctr_threshold,
        'ctr_threshold_inr', d.ctr_threshold,
        'window_days', d.window_days
    )                                                               AS detail,
    MIN(d.txn_timestamp)                                            AS first_txn_at,
    MAX(d.txn_timestamp)                                            AS last_txn_at,
    {{ r.id }} || '|' || UPPER(d.party_id) || '|'
        || TO_VARCHAR(DATEADD('day', (d.bucket * d.window_days)::INT, {{ r.anchor }})) AS signal_business_key
FROM (
    SELECT
        e.party_id,
        e.account_id,
        e.txn_id,
        e.txn_timestamp,
        e.amount_inr,
        FLOOR(DATEDIFF('day', {{ r.anchor }}, e.txn_date) / c.window_days) AS bucket,
        c.ctr_threshold,
        c.band_lower,
        c.band_upper,
        c.min_accounts,
        c.window_days
    FROM {{ fq.curated }}.{{ dt.transactions_enriched.name }} e
    CROSS JOIN (
        SELECT
            MAX(IFF(clause_id = {{ r.clause.ctr }}, threshold_lower, NULL))          AS ctr_threshold,
            MAX(IFF(clause_id = {{ r.clause.band }}, threshold_lower, NULL))         AS band_lower,
            MAX(IFF(clause_id = {{ r.clause.band }}, threshold_upper, NULL))         AS band_upper,
            MAX(IFF(clause_id = {{ r.clause.min_accounts }}, threshold_lower, NULL)) AS min_accounts,
            MAX(IFF(clause_id = {{ r.clause.window }}, threshold_lower, NULL))       AS window_days
        FROM {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT
    ) c
    WHERE e.leg_side = 'C'
      AND e.is_cash
      AND NOT e.is_internal_account
      AND e.amount_inr BETWEEN c.band_lower * c.ctr_threshold AND c.band_upper * c.ctr_threshold
) d
GROUP BY d.party_id, d.bucket, d.window_days, d.min_accounts, d.ctr_threshold, d.band_lower, d.band_upper
HAVING COUNT(DISTINCT d.account_id) >= d.min_accounts
