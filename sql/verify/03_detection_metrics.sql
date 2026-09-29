-- ===========================================================================
-- verify / 03_detection_metrics.sql -- admin only (reads GROUND_TRUTH)
-- ===========================================================================
-- Per-rule recall and precision at party-month, the grain
-- EVAL_GRAIN_PARTY_MONTH declares. Reported, not gated.
--
-- The grain mismatch (D40): a signal is keyed by its rule's window bucket (a
-- 30-day tumbling window), the grain by calendar month. A signal counts in
-- every party-month holding one of its evidence transactions, month taken
-- from the transaction timestamp in the format generation used to build the
-- grain. The window's start month would put a 2022-08-26 bucket in August,
-- where its deposits are not.
--
--   predicted       party-months some signal of the rule lands in
--   expected        party-months whose expected_rule_ids name the rule
--   false_positive  predicted, not expected: legit, illicit of another
--                   typology, or not in the grain at all (should be 0)
--   controls        per rule, sql/verify/controls/<rule_id>.sql: party-months
--                   holding a genuine near-miss, the false-positive control
--   evidence_unresolved  evidence ids with no RAW transaction (should be 0)
-- ===========================================================================
{% from "verify/_macros.sql" import signals, registered with context %}
WITH rules AS (
{{ registered() }}
),
grain AS (
    SELECT party_id, month_bucket, is_illicit_actual, expected_rule_ids
    FROM {{ fq.ground_truth }}.EVAL_GRAIN_PARTY_MONTH
),
evidence AS (
    SELECT s.rule_id, s.party_id, f.value::VARCHAR AS txn_id
    FROM (
{{ signals("rule_id, party_id, evidence_txn_ids") }}
    ) s,
    LATERAL FLATTEN(input => s.evidence_txn_ids) f
),
predicted AS (
    SELECT DISTINCT e.rule_id, e.party_id,
           TO_CHAR(t.txn_timestamp, {{ eval_grain.month_format }}) AS month_bucket
    FROM evidence e
    JOIN {{ fq.raw }}.RAW_TRANSACTIONS t ON t.txn_id = e.txn_id
),
unresolved AS (
    SELECT e.rule_id, COUNT(*) AS evidence_unresolved
    FROM evidence e
    LEFT JOIN {{ fq.raw }}.RAW_TRANSACTIONS t ON t.txn_id = e.txn_id
    WHERE t.txn_id IS NULL
    GROUP BY e.rule_id
),
expected AS (
    SELECT r.rule_id, g.party_id, g.month_bucket
    FROM rules r
    JOIN grain g ON ARRAY_CONTAINS(r.rule_id::VARIANT, g.expected_rule_ids)
),
cells AS (
    SELECT
        COALESCE(p.rule_id, e.rule_id)          AS rule_id,
        COALESCE(p.party_id, e.party_id)        AS party_id,
        COALESCE(p.month_bucket, e.month_bucket) AS month_bucket,
        p.rule_id IS NOT NULL                   AS predicted,
        e.rule_id IS NOT NULL                   AS expected
    FROM predicted p
    FULL OUTER JOIN expected e
        ON e.rule_id = p.rule_id AND e.party_id = p.party_id AND e.month_bucket = p.month_bucket
),
scored AS (
    SELECT
        r.rule_id,
        COUNT_IF(c.predicted)                                           AS predicted,
        COUNT_IF(c.expected)                                            AS expected,
        COUNT_IF(c.predicted AND c.expected)                            AS true_positive,
        COUNT_IF(c.predicted AND NOT c.expected)                        AS false_positive,
        COUNT_IF(c.predicted AND NOT c.expected AND NOT g.is_illicit_actual) AS fp_legit,
        COUNT_IF(c.predicted AND NOT c.expected AND g.is_illicit_actual)     AS fp_other_illicit,
        COUNT_IF(c.predicted AND g.party_id IS NULL)                    AS not_in_grain,
        COUNT_IF(c.expected AND NOT c.predicted)                        AS missed
    FROM rules r
    LEFT JOIN cells c ON c.rule_id = r.rule_id
    LEFT JOIN grain g ON g.party_id = c.party_id AND g.month_bucket = c.month_bucket
    GROUP BY r.rule_id
),
controls AS (
{% for rule_id, spec in rule | dictsort %}
    SELECT {{ spec.id }} AS rule_id, k.party_id, k.month_bucket, k.defined
    FROM (
{% include ["verify/controls/" ~ rule_id ~ ".sql", "verify/controls/_none.sql"] %}

    ) k
{{ "    UNION ALL" if not loop.last }}
{% endfor %}
),
control_scored AS (
    SELECT
        k.rule_id,
        BOOLOR_AGG(k.defined)                                           AS control_defined,
        COUNT(DISTINCT IFF(k.defined, k.party_id, NULL), k.month_bucket) AS controls,
        COUNT(DISTINCT IFF(k.defined AND p.rule_id IS NOT NULL, k.party_id, NULL), k.month_bucket)
                                                                        AS controls_fired
    FROM controls k
    LEFT JOIN predicted p
        ON p.rule_id = k.rule_id AND p.party_id = k.party_id AND p.month_bucket = k.month_bucket
    GROUP BY k.rule_id
)
SELECT
    s.rule_id,
    s.predicted,
    s.expected,
    s.true_positive,
    s.false_positive,
    s.fp_legit,
    s.fp_other_illicit,
    s.not_in_grain,
    s.missed,
    s.true_positive / NULLIF(s.expected, 0)  AS recall,
    s.true_positive / NULLIF(s.predicted, 0) AS precision,
    COALESCE(u.evidence_unresolved, 0)       AS evidence_unresolved,
    k.control_defined,
    k.controls,
    k.controls_fired
FROM scored s
LEFT JOIN unresolved u ON u.rule_id = s.rule_id
LEFT JOIN control_scored k ON k.rule_id = s.rule_id
ORDER BY s.rule_id;
