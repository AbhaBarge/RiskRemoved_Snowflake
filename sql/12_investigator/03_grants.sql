-- ===========================================================================
-- 12_investigator / 03_grants.sql
-- ===========================================================================
-- Who may talk to the investigator: the service role (Slack bridge), the
-- compliance officer, and the hosted app's role. Each needs USAGE on the
-- agent and its case-context procedure, and SELECT on the semantic view the
-- Analyst tool queries (the app role already holds it from 11_app_access;
-- granting again is a no-op). The procedure runs as owner, so no table grant
-- is added here.
-- ===========================================================================
{% set agent_fq = fq.evidence ~ "." ~ cortex.investigator_agent %}
{% set ctx_proc = fq.evidence ~ "." ~ cortex.investigator_context_proc %}
{% set sv_fq = fq.curated ~ "." ~ cortex.semantic_view %}
{% set grantees = [role.service, role.compliance_officer] + ([role.public_app] if role.public_app else []) %}

{% for r in grantees %}
GRANT USAGE ON AGENT {{ agent_fq }} TO ROLE {{ r }};
GRANT USAGE ON PROCEDURE {{ ctx_proc }}(VARCHAR) TO ROLE {{ r }};
GRANT SELECT ON SEMANTIC VIEW {{ sv_fq }} TO ROLE {{ r }};
{% endfor %}
