-- ===========================================================================
-- verify / 05_hero.sql
-- ===========================================================================
-- For each rule the hero pin must trip: did the hero fire it, under which
-- signal_business_key(s), and on which case (if the signal is routed yet --
-- verify reads CASE_SIGNAL and never routes, D39).
--
-- Binds: the pin's must_trigger_rules as a JSON array, the pin's party_id.
-- ===========================================================================
{% from "verify/_macros.sql" import signals with context %}
WITH must AS (
    SELECT f.value::VARCHAR AS rule_id
    FROM TABLE(FLATTEN(input => PARSE_JSON(?))) f
),
fired AS (
    SELECT rule_id, signal_business_key
    FROM (
{{ signals("rule_id, party_id, signal_business_key") }}
    )
    WHERE party_id = ?
)
SELECT
    m.rule_id,
    COUNT(x.signal_business_key) > 0 AS detected,
    ARRAY_AGG(DISTINCT x.signal_business_key) WITHIN GROUP (ORDER BY x.signal_business_key)
        AS signal_business_keys,
    ARRAY_AGG(DISTINCT cs.case_id) WITHIN GROUP (ORDER BY cs.case_id) AS case_ids
FROM must m
LEFT JOIN fired x ON x.rule_id = m.rule_id
LEFT JOIN {{ fq.evidence }}.SIGNAL s ON s.signal_business_key = x.signal_business_key
LEFT JOIN {{ fq.evidence }}.CASE_SIGNAL cs ON cs.signal_id = s.signal_id
GROUP BY m.rule_id
ORDER BY m.rule_id;
