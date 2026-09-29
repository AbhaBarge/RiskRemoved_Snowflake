{#- sql/verify/controls/_none.sql -- stands in for a rule with no control file:
    one marker row with defined = FALSE, so the report says "no control"
    rather than "0 controls". -#}
SELECT NULL::VARCHAR AS party_id, NULL::VARCHAR AS month_bucket, FALSE AS defined
