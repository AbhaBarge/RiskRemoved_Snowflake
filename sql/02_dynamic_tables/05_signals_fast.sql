-- ===========================================================================
-- 02_dynamic_tables / 05_signals_fast.sql
-- ===========================================================================
-- Every rule conf/detection.yml assigns to signals_fast, one UNION ALL branch
-- each, from sql/rules/<rule_id>.sql. Built:
{% for rule_id, spec in rule | dictsort if spec.dynamic_table == "signals_fast" %}
--   {{ rule_id }} v{{ spec.version }}
{% endfor %}
--
-- One row per (rule, party, window): recomputed on every refresh, so the same
-- logical signal reappears each time. signal_business_key is what the router
-- MERGEs on, which is what makes that harmless (GAP-11).
--
-- rule_version and severity are NOT carried here; the router takes both from
-- RULE_DEFINITION, the registry the policy stage governs.
-- ===========================================================================
{% from "02_dynamic_tables/_macros.sql" import target_lag %}

CREATE OR ALTER DYNAMIC TABLE {{ fq.curated }}.{{ dt.signals_fast.name }}
    TARGET_LAG = {{ target_lag(dt.signals_fast) }}
    WAREHOUSE = {{ wh.pipeline }}
    REFRESH_MODE = INCREMENTAL
    COMMENT = 'Rule firings at party-window grain, keyed by signal_business_key. Routed by ROUTE_SIGNALS.'
AS
{% for rule_id, spec in rule | dictsort if spec.dynamic_table == "signals_fast" %}
{% include "rules/" ~ rule_id ~ ".sql" %}
{{ "UNION ALL" if not loop.last else "" }}
{% endfor %}
;
