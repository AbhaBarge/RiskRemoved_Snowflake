{#- ==========================================================================
    02_dynamic_tables / _macros.sql -- imported, never deployed on its own
    (Deployer.stage_files skips files starting with "_").
    ========================================================================== -#}

{#- TARGET_LAG takes a quoted duration or the bare keyword DOWNSTREAM. -#}
{% macro target_lag(spec) -%}
{{ spec.target_lag if spec.target_lag == "DOWNSTREAM" else "'" ~ spec.target_lag ~ "'" }}
{%- endmacro %}
