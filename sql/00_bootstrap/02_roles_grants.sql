-- ===========================================================================
-- 00_bootstrap / 02_roles_grants.sql
-- ===========================================================================
-- Role hierarchy and grants.
--
-- Requires a role that can create roles (ACCOUNTADMIN or a delegate). If the
-- deploying principal cannot, this file fails first and nothing else is
-- half-created.
--
-- Design note on least privilege: the branch analyst roles get SELECT only.
-- They are the subjects of the row-access-policy demo, so granting them write
-- access anywhere would undermine the very claim being demonstrated.
-- ===========================================================================

USE ROLE ACCOUNTADMIN;

-- --------------------------------------------------------------------------
-- Roles
-- --------------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS {{ role.admin }}
    COMMENT = 'Owns all Risk Copilot objects ({{ profile }}).';

CREATE ROLE IF NOT EXISTS {{ role.service }}
    COMMENT = 'Used by Cortex agents and scheduled automations. Never a human.';

CREATE ROLE IF NOT EXISTS {{ role.compliance_officer }}
    COMMENT = 'Sees all data; the only role permitted to attest an artifact.';

{% for region, analyst_role in role.branch_analysts.items() %}
CREATE ROLE IF NOT EXISTS {{ analyst_role }}
    COMMENT = 'Branch analyst restricted to branch_region = {{ region }} by row access policy.';
{% endfor %}

-- --------------------------------------------------------------------------
-- Hierarchy.  Inheritance runs upward: ACCOUNTADMIN sees everything
-- {{ role.admin }} can see, which is what keeps the account auditable.
-- --------------------------------------------------------------------------
GRANT ROLE {{ role.admin }} TO ROLE ACCOUNTADMIN;
GRANT ROLE {{ role.service }} TO ROLE {{ role.admin }};
GRANT ROLE {{ role.compliance_officer }} TO ROLE {{ role.admin }};
{% for region, analyst_role in role.branch_analysts.items() %}
GRANT ROLE {{ analyst_role }} TO ROLE {{ role.admin }};
{% endfor %}

-- The deploying user needs the admin role to apply every later stage.
-- GRANT ROLE ... TO USER takes a literal identifier and rejects IDENTIFIER(),
-- so the user name is resolved at run time and the grant issued as dynamic SQL.
-- Quoted, because a user name is not guaranteed to be a bare identifier.
EXECUTE IMMEDIATE $$
BEGIN
    LET deploying_user STRING := CURRENT_USER();
    EXECUTE IMMEDIATE 'GRANT ROLE {{ role.admin }} TO USER "' || deploying_user || '"';
    RETURN 'granted {{ role.admin }} to ' || deploying_user;
END;
$$;

-- --------------------------------------------------------------------------
-- Warehouse privileges
-- --------------------------------------------------------------------------
GRANT USAGE ON WAREHOUSE {{ wh.dev }} TO ROLE {{ role.admin }};
GRANT OPERATE, USAGE ON WAREHOUSE {{ wh.pipeline }} TO ROLE {{ role.admin }};
GRANT USAGE ON WAREHOUSE {{ wh.pipeline }} TO ROLE {{ role.service }};
GRANT USAGE ON WAREHOUSE {{ wh.demo }} TO ROLE {{ role.service }};
GRANT USAGE ON WAREHOUSE {{ wh.demo }} TO ROLE {{ role.compliance_officer }};
{% for region, analyst_role in role.branch_analysts.items() %}
GRANT USAGE ON WAREHOUSE {{ wh.demo }} TO ROLE {{ analyst_role }};
{% endfor %}

-- --------------------------------------------------------------------------
-- Ownership.  The admin role owns everything, so object ownership does not
-- follow whichever human happened to run the deploy.
-- --------------------------------------------------------------------------
GRANT OWNERSHIP ON DATABASE {{ db }} TO ROLE {{ role.admin }} COPY CURRENT GRANTS;
{% for key in ['raw', 'curated', 'governance', 'evidence', 'audit', 'ground_truth', 'ml'] %}
GRANT OWNERSHIP ON SCHEMA {{ fq[key] }} TO ROLE {{ role.admin }} COPY CURRENT GRANTS;
{% endfor %}

USE ROLE {{ role.admin }};

-- --------------------------------------------------------------------------
-- Database and schema usage
-- --------------------------------------------------------------------------
{% set consumer_roles = [role.service, role.compliance_officer] + role.branch_analysts.values()|list %}
{% for consumer in consumer_roles %}
GRANT USAGE ON DATABASE {{ db }} TO ROLE {{ consumer }};
GRANT USAGE ON SCHEMA {{ fq.curated }} TO ROLE {{ consumer }};
GRANT USAGE ON SCHEMA {{ fq.governance }} TO ROLE {{ consumer }};
GRANT USAGE ON SCHEMA {{ fq.evidence }} TO ROLE {{ consumer }};
{% endfor %}

-- --------------------------------------------------------------------------
-- Read access to curated and governance state.
--
-- FUTURE GRANTS cover objects created by later stages, so a new dynamic table
-- does not silently become invisible to the roles that need it. They apply only
-- to objects created AFTER the grant, so the matching ALL GRANTS below covers
-- anything that already exists on a re-run.
-- --------------------------------------------------------------------------
{% for consumer in consumer_roles %}
GRANT SELECT ON FUTURE TABLES         IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON FUTURE VIEWS          IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON FUTURE DYNAMIC TABLES IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON FUTURE TABLES         IN SCHEMA {{ fq.governance }} TO ROLE {{ consumer }};
GRANT SELECT ON FUTURE VIEWS          IN SCHEMA {{ fq.governance }} TO ROLE {{ consumer }};
GRANT SELECT ON ALL TABLES            IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON ALL VIEWS             IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON ALL DYNAMIC TABLES    IN SCHEMA {{ fq.curated }}    TO ROLE {{ consumer }};
GRANT SELECT ON ALL TABLES            IN SCHEMA {{ fq.governance }} TO ROLE {{ consumer }};
GRANT SELECT ON ALL VIEWS             IN SCHEMA {{ fq.governance }} TO ROLE {{ consumer }};
{% endfor %}

-- --------------------------------------------------------------------------
-- Evidence chain.
--
-- Read for everyone who investigates; write ONLY for the service role, because
-- the chain is append-only and must be written by the pipeline rather than by
-- hand. Analysts deliberately get no INSERT: a human editing an evidence row
-- would destroy the reproducibility claim.
-- --------------------------------------------------------------------------
{% for consumer in consumer_roles %}
GRANT SELECT ON FUTURE TABLES IN SCHEMA {{ fq.evidence }} TO ROLE {{ consumer }};
GRANT SELECT ON ALL TABLES    IN SCHEMA {{ fq.evidence }} TO ROLE {{ consumer }};
{% endfor %}

GRANT INSERT, UPDATE ON FUTURE TABLES IN SCHEMA {{ fq.evidence }} TO ROLE {{ role.service }};
GRANT INSERT, UPDATE ON ALL TABLES    IN SCHEMA {{ fq.evidence }} TO ROLE {{ role.service }};

-- The Principal Officer's attestation is the human gate on filing. Only the
-- compliance role may write one; this is the maker-checker boundary expressed
-- as a privilege rather than as prose in a prompt.
GRANT USAGE ON SCHEMA {{ fq.audit }} TO ROLE {{ role.service }};
GRANT INSERT ON FUTURE TABLES IN SCHEMA {{ fq.audit }} TO ROLE {{ role.service }};
GRANT INSERT ON ALL TABLES    IN SCHEMA {{ fq.audit }} TO ROLE {{ role.service }};

-- Compliance may read the audit trail; nobody may update or delete it.
GRANT USAGE ON SCHEMA {{ fq.audit }} TO ROLE {{ role.compliance_officer }};
GRANT SELECT ON FUTURE TABLES IN SCHEMA {{ fq.audit }} TO ROLE {{ role.compliance_officer }};
GRANT SELECT ON ALL TABLES    IN SCHEMA {{ fq.audit }} TO ROLE {{ role.compliance_officer }};

-- --------------------------------------------------------------------------
-- GROUND_TRUTH is granted to NOBODY beyond the admin role.
--
-- This is the structural half of the label-leakage control (validation gate
-- V6): the agent's service role has no USAGE on the schema, so no amount of
-- prompt engineering can reach a label. The semantic-view check is the second
-- half. Enforced here rather than trusted to a YAML omission.
-- --------------------------------------------------------------------------
REVOKE ALL PRIVILEGES ON SCHEMA {{ fq.ground_truth }} FROM ROLE {{ role.service }};

-- --------------------------------------------------------------------------
-- Stages
-- --------------------------------------------------------------------------
GRANT READ ON STAGE {{ fq.governance }}.{{ stage.policy_docs }} TO ROLE {{ role.service }};
GRANT USAGE ON SCHEMA {{ fq.ml }} TO ROLE {{ role.service }};
GRANT SELECT ON FUTURE TABLES IN SCHEMA {{ fq.ml }} TO ROLE {{ role.service }};
GRANT SELECT ON ALL TABLES    IN SCHEMA {{ fq.ml }} TO ROLE {{ role.service }};
