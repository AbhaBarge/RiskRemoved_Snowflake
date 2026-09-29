-- ===========================================================================
-- verify / 02_signal_yield.sql
-- ===========================================================================
-- Signals per rule against validation.signal_yield. Every registered rule is
-- listed, so a rule that fired nothing shows as 0 (and fails) rather than
-- vanishing; a rule id in a signal table that is not registered also fails.
--
-- Binds: min_detections_per_rule, max_detections_per_rule.
-- ===========================================================================
{% from "verify/_macros.sql" import signals, registered with context %}
WITH registered AS (
{{ registered() }}
),
fired AS (
    SELECT rule_id, COUNT(*) AS signals
    FROM (
{{ signals("rule_id") }}
    )
    GROUP BY rule_id
),
bounds AS (
    SELECT ?::NUMBER(38,0) AS min_signals, ?::NUMBER(38,0) AS max_signals
)
SELECT
    COALESCE(r.rule_id, f.rule_id)          AS rule_id,
    r.rule_id IS NOT NULL                   AS registered,
    COALESCE(f.signals, 0)                  AS signals,
    b.min_signals,
    b.max_signals,
    r.rule_id IS NOT NULL
        AND COALESCE(f.signals, 0) BETWEEN b.min_signals AND b.max_signals AS within_bounds
FROM registered r
FULL OUTER JOIN fired f ON f.rule_id = r.rule_id
CROSS JOIN bounds b
ORDER BY 1;
