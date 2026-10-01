{#- ==========================================================================
    sql/procedures/_macros.sql -- Snowflake Scripting fragments shared by the
    case tools. Imported `with context`, never deployed on its own.

    Callers declare: wrapped VARCHAR, t0 TIMESTAMP_LTZ,
    snap VARIANT, ql_ms INTEGER, and (for audit) started TIMESTAMP_LTZ.
    ========================================================================== -#}

{#- Canonical form of one logged evidence query, tag SQL-1. Every row becomes an
    object (OBJECT_CONSTRUCT_KEEP_NULL, so NULLs are kept as JSON null), rows are
    ordered by their own JSON text, and the hash is SHA-256 of that array. Row
    order and column order therefore cannot change the hash; a changed value
    can. GATHER_EVIDENCE and REVIEW_STR both compute it through this one macro,
    which is what makes "recompute and compare" meaningful. -#}
{% set canonical_form = "SQL-1" %}

{#- Run query text `qvar` with the single bind `bind`, leaving
    {row_count, result_columns, result_snapshot, result_hash} in `snap`. -#}
{% macro snapshot(qvar, bind) -%}
wrapped := 'SELECT OBJECT_INSERT(x, ''result_hash'', SHA2(TO_JSON(x:result_snapshot), 256)) AS r FROM (SELECT OBJECT_CONSTRUCT(''row_count'', COUNT(*), ''result_columns'', COALESCE(OBJECT_KEYS(ANY_VALUE(o)), ARRAY_CONSTRUCT()), ''result_snapshot'', COALESCE(ARRAY_AGG(o) WITHIN GROUP (ORDER BY TO_JSON(o)), ARRAY_CONSTRUCT())) AS x FROM (SELECT OBJECT_CONSTRUCT_KEEP_NULL(*) AS o FROM (' || {{ qvar }} || ') q) w) z';
        t0 := CURRENT_TIMESTAMP();
        EXECUTE IMMEDIATE :wrapped USING ({{ bind }});
        snap := (SELECT r FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())));
        ql_ms := DATEDIFF('millisecond', t0, CURRENT_TIMESTAMP());
{%- endmacro %}

{#- One AGENT_ACTION_LOG row. `args` and `evidence` are SQL expressions. The
    procedures run with owner's rights, so CURRENT_ROLE() is the owning role;
    the caller is CURRENT_USER(). -#}
{% macro audit(mode, tool, args, outcome, model, prompt, evidence="NULL", error="NULL", case_var="case_id") -%}
INSERT INTO {{ fq.audit }}.AGENT_ACTION_LOG (
        log_id, session_id, turn_number, agent_mode, invoked_by, snowflake_user, user_role,
        tools_invoked, tool_arguments, case_ids_touched, evidence_ids_accessed,
        model_version, prompt_version, latency_ms, outcome, error_text
    )
    SELECT 'LOG-' || UUID_STRING(), CURRENT_SESSION()::VARCHAR,
           (SELECT COUNT(*) + 1 FROM {{ fq.audit }}.AGENT_ACTION_LOG WHERE session_id = CURRENT_SESSION()::VARCHAR),
           '{{ mode }}', 'USER', CURRENT_USER(), CURRENT_ROLE(),
           ARRAY_CONSTRUCT('{{ tool }}'), {{ args }}, ARRAY_CONSTRUCT(:{{ case_var }}), {{ evidence }},
           {{ model }}, {{ prompt }}, DATEDIFF('millisecond', :started, CURRENT_TIMESTAMP()),
           '{{ outcome }}', {{ error }}
{%- endmacro %}
