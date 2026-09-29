{#- ==========================================================================
    sql/verify/_macros.sql -- fragments shared by the verify queries. Not run on
    its own. Import WITH CONTEXT: the macros read `rule`, `dt` and `fq`.
    ========================================================================== -#}

{#- The signal tables, one per dynamic_tables key some registered rule writes to. -#}
{% macro signal_tables() -%}
{% for key in rule.values() | map(attribute="dynamic_table") | unique | sort %}
{{ fq.curated }}.{{ dt[key].name }}{{ "," if not loop.last }}
{% endfor %}
{%- endmacro %}

{#- `cols` from every signal table, as one relation. -#}
{% macro signals(cols) -%}
{% for key in rule.values() | map(attribute="dynamic_table") | unique | sort %}
SELECT {{ cols }} FROM {{ fq.curated }}.{{ dt[key].name }}
{{ "UNION ALL" if not loop.last }}
{% endfor %}
{%- endmacro %}

{#- The registered rule ids (conf/detection.yml `rules`), one row each. -#}
{% macro registered() -%}
{% for rule_id, spec in rule | dictsort %}
SELECT {{ spec.id }} AS rule_id
{{ "UNION ALL" if not loop.last }}
{% endfor %}
{%- endmacro %}
