-- ===========================================================================
-- 00_bootstrap / 01_account_objects.sql
-- ===========================================================================
-- Database, schemas, warehouses and stages. Every identifier is templated: see
-- sql/README.md for the sql_context contract. A literal object name here is a
-- deploy-time error, because it would defeat dev/demo separation.
--
-- Idempotent throughout. Re-running is the normal way to apply a change.
-- ===========================================================================

-- Account-level creation (database, warehouses) needs ACCOUNTADMIN, and the
-- admin role this project creates deliberately does not hold CREATE DATABASE.
-- Set explicitly rather than inherited, because on a re-run the session role is
-- already the admin role -- which would fail here. 02_roles_grants.sql then
-- transfers ownership of everything created here to the admin role.
USE ROLE ACCOUNTADMIN;

-- --------------------------------------------------------------------------
-- Database
-- --------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS {{ db }}
    COMMENT = 'Risk & Compliance Copilot ({{ profile }} profile). Synthetic data only.';

-- Landing tables are append-only, so a longer Time Travel window is cheap and
-- is what makes the Reviewer''s as_of_ts reachable. Correctness does not depend
-- on it -- EVIDENCE_QUERY_LOG stores a result snapshot -- but provenance
-- re-verification does, and the one-day default would silently expire evidence
-- mid-demo.
ALTER DATABASE {{ db }} SET DATA_RETENTION_TIME_IN_DAYS = {{ retention.default_days }};

-- --------------------------------------------------------------------------
-- Schemas.  MANAGED ACCESS means only the schema owner grants privileges on
-- its objects, so an object owner cannot quietly widen access -- the audit
-- story depends on grants being centrally controlled.
-- --------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS {{ fq.raw }}
    WITH MANAGED ACCESS
    DATA_RETENTION_TIME_IN_DAYS = {{ retention.landing_days }}
    COMMENT = 'Immutable landing. Append-only; never updated in place.';

CREATE SCHEMA IF NOT EXISTS {{ fq.curated }}
    WITH MANAGED ACCESS
    COMMENT = 'Dynamic tables and dimensions. Derived; safe to rebuild.';

CREATE SCHEMA IF NOT EXISTS {{ fq.governance }}
    WITH MANAGED ACCESS
    COMMENT = 'POLICY_CLAUSE, TYPOLOGY, RULE_DEFINITION, REF_*. Human-gated state.';

CREATE SCHEMA IF NOT EXISTS {{ fq.evidence }}
    WITH MANAGED ACCESS
    DATA_RETENTION_TIME_IN_DAYS = {{ retention.evidence_days }}
    COMMENT = 'SIGNAL -> CASE -> EVIDENCE -> FINDING -> ARTIFACT -> ATTESTATION. Append-only.';

CREATE SCHEMA IF NOT EXISTS {{ fq.audit }}
    WITH MANAGED ACCESS
    DATA_RETENTION_TIME_IN_DAYS = {{ retention.evidence_days }}
    COMMENT = 'AGENT_ACTION_LOG. Append-only; never updated or deleted.';

-- Isolated deliberately: the labels live here, and the leakage gate asserts
-- that nothing in this schema is reachable from the semantic view.
CREATE SCHEMA IF NOT EXISTS {{ fq.ground_truth }}
    WITH MANAGED ACCESS
    COMMENT = 'Evaluation labels. NEVER exposed in the semantic view or to the agent.';

CREATE SCHEMA IF NOT EXISTS {{ fq.ml }}
    WITH MANAGED ACCESS
    COMMENT = 'ML models and scoring output. Scores rank work; they are not evidence.';

-- --------------------------------------------------------------------------
-- Warehouses.  Separated by workload so pipeline refreshes cannot starve demo
-- queries, and so credit consumption is attributable per workload.
-- --------------------------------------------------------------------------
{% for key, spec in wh_spec.items() %}
CREATE WAREHOUSE IF NOT EXISTS {{ wh[key] }}
    WAREHOUSE_SIZE = {{ spec.size }}
    AUTO_SUSPEND = {{ spec.auto_suspend_secs }}
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    COMMENT = '{{ spec.purpose }}';

-- Applied separately from CREATE ... IF NOT EXISTS: that form is a no-op when
-- the warehouse already exists, so a size change in config would never take
-- effect without this.
ALTER WAREHOUSE {{ wh[key] }} SET
    WAREHOUSE_SIZE = {{ spec.size }}
    AUTO_SUSPEND = {{ spec.auto_suspend_secs }};
{% endfor %}

-- --------------------------------------------------------------------------
-- Stages.  SNOWFLAKE_SSE server-side encryption is required for stages read by
-- AI_PARSE_DOCUMENT and by Python stored procedures.
-- --------------------------------------------------------------------------
CREATE STAGE IF NOT EXISTS {{ fq.raw }}.{{ stage.data }}
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Source datasets: AMLworld, OpenSanctions. See data/MANIFEST.yml.';

CREATE STAGE IF NOT EXISTS {{ fq.raw }}.{{ stage.generated }}
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Output of the L1/L2/L3 generators, staged for COPY INTO.';

CREATE STAGE IF NOT EXISTS {{ fq.governance }}.{{ stage.policy_docs }}
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Regulatory PDFs for AI_PARSE_DOCUMENT.';

CREATE STAGE IF NOT EXISTS {{ fq.governance }}.{{ stage.scripts }}
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Python payloads for stored procedures.';

-- --------------------------------------------------------------------------
-- File formats.  Defined once here rather than inline in each COPY INTO, so the
-- duplicate-header handling for the AMLworld transaction file cannot drift
-- between load scripts.
-- --------------------------------------------------------------------------
CREATE FILE FORMAT IF NOT EXISTS {{ fq.raw }}.FF_CSV_HEADER
    TYPE = CSV
    PARSE_HEADER = FALSE
    SKIP_HEADER = 1
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    NULL_IF = ('', 'NULL', 'null', '\\N')
    EMPTY_FIELD_AS_NULL = TRUE
    TRIM_SPACE = TRUE
    COMMENT = 'Header skipped so positional $1..$n references work. Required for HI-Small_Trans.csv, whose columns 3 and 5 share the header "Account".';

CREATE FILE FORMAT IF NOT EXISTS {{ fq.raw }}.FF_CSV_SEMICOLON_MULTIVALUE
    TYPE = CSV
    PARSE_HEADER = FALSE
    SKIP_HEADER = 1
    FIELD_DELIMITER = ','
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    NULL_IF = ('', 'NULL')
    EMPTY_FIELD_AS_NULL = TRUE
    COMMENT = 'OpenSanctions targets.simple.csv. Multi-value fields are semicolon-delimited WITHIN a comma-delimited field; split in SQL.';

CREATE FILE FORMAT IF NOT EXISTS {{ fq.raw }}.FF_PARQUET
    TYPE = PARQUET
    COMMENT = 'Generator output. Preferred over CSV: carries types and compresses.';

CREATE FILE FORMAT IF NOT EXISTS {{ fq.raw }}.FF_NDJSON
    TYPE = JSON
    STRIP_OUTER_ARRAY = FALSE
    COMMENT = 'Newline-delimited JSON (OpenSanctions FtM). Future scope.';
