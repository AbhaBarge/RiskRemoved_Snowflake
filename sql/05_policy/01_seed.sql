-- ===========================================================================
-- 05_policy / 01_seed.sql
-- ===========================================================================
-- conf/detection.yml into the GOVERNANCE tables. The ONLY place threshold
-- values enter Snowflake: as rows of POLICY_CLAUSE, which every detection query
-- joins to. Nothing in this file is a value; every literal comes from
-- policy_seed in the render context (src/risk_copilot/policy.py), which
-- validates provenance, clause references and severities before rendering.
--
-- Governed, versioned tables are INSERT-ONLY. If a (clause_id, clause_version)
-- or (rule_id, rule_version) already exists and differs from the seed, the
-- drift check fails the deploy BEFORE anything is written: a threshold changes
-- through a new version (the POLICY_CHANGE_PROPOSALS gate), never by editing
-- the seed and redeploying over a live row.
--
-- Reference catalogues (TYPOLOGY, REF_CHANNEL) are unversioned and upserted.
--
-- The seed rows are staged in session-scoped TEMPORARY tables so each is
-- written once and read by both the drift check and the MERGE.
-- ===========================================================================

{% macro seed_select(rows) -%}
{% for row in rows %}
SELECT {% for column, literal in row.items() %}{{ literal }} AS {{ column }}{{ ", " if not loop.last else "" }}{% endfor %}{{ "\nUNION ALL" if not loop.last else "" }}
{% endfor %}
{%- endmacro %}

USE SCHEMA {{ fq.governance }};

-- --------------------------------------------------------------------------
-- POLICY_CLAUSE -- insert-only, drift fails
-- --------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE SEED_POLICY_CLAUSE AS
{{ seed_select(policy_seed.clauses) }};

EXECUTE IMMEDIATE $$
DECLARE
    clause_drift EXCEPTION (-20001, 'POLICY_CLAUSE: a live clause version differs from conf/detection.yml. Supersede it with a new clause_version; the seed never overwrites a governed row.');
BEGIN
    LET drifted INTEGER := (
        SELECT COUNT(*)
        FROM SEED_POLICY_CLAUSE s
        JOIN POLICY_CLAUSE t
          ON t.clause_id = s.clause_id AND t.clause_version = s.clause_version
        WHERE NOT (
                EQUAL_NULL(t.source_document, s.source_document)
            AND EQUAL_NULL(t.source_page, s.source_page)
            AND EQUAL_NULL(t.section_heading, s.section_heading)
            AND EQUAL_NULL(t.clause_text, s.clause_text)
            AND EQUAL_NULL(t.metric, s.metric)
            AND EQUAL_NULL(t.operator, s.operator)
            AND EQUAL_NULL(t.threshold_lower, s.threshold_lower)
            AND EQUAL_NULL(t.threshold_upper, s.threshold_upper)
            AND EQUAL_NULL(t.action_required, s.action_required)
            AND EQUAL_NULL(t.effective_from, s.effective_from)
            AND EQUAL_NULL(t.effective_to, s.effective_to)
            AND EQUAL_NULL(t.supersedes_clause_id, s.supersedes_clause_id)
        )
    );
    IF (drifted > 0) THEN
        RAISE clause_drift;
    END IF;
    RETURN 'POLICY_CLAUSE seed: no drift';
END;
$$;

MERGE INTO POLICY_CLAUSE t
USING SEED_POLICY_CLAUSE s
   ON t.clause_id = s.clause_id AND t.clause_version = s.clause_version
WHEN NOT MATCHED THEN INSERT (
    clause_id, clause_version, source_document, source_page, section_heading, clause_text,
    metric, operator, threshold_lower, threshold_upper, action_required,
    effective_from, effective_to, supersedes_clause_id
) VALUES (
    s.clause_id, s.clause_version, s.source_document, s.source_page, s.section_heading, s.clause_text,
    s.metric, s.operator, s.threshold_lower, s.threshold_upper, s.action_required,
    s.effective_from, s.effective_to, s.supersedes_clause_id
);

-- --------------------------------------------------------------------------
-- TYPOLOGY -- reference catalogue, upserted
-- --------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE SEED_TYPOLOGY AS
{{ seed_select(policy_seed.typologies) }};

MERGE INTO TYPOLOGY t
USING SEED_TYPOLOGY s
   ON t.typology_id = s.typology_id
WHEN MATCHED THEN UPDATE SET
    name = s.name,
    fatf_reference = s.fatf_reference,
    rbi_reference = s.rbi_reference,
    description = s.description,
    detection_approach = s.detection_approach,
    amlworld_pattern = s.amlworld_pattern
WHEN NOT MATCHED THEN INSERT (
    typology_id, name, fatf_reference, rbi_reference, description, detection_approach, amlworld_pattern
) VALUES (
    s.typology_id, s.name, s.fatf_reference, s.rbi_reference, s.description, s.detection_approach,
    s.amlworld_pattern
);

-- --------------------------------------------------------------------------
-- RULE_DEFINITION -- insert-only, drift fails. After TYPOLOGY: the foreign key
-- is not enforced by Snowflake, so the order is what keeps it true.
-- rule_sql_hash is bound when the rule's detection SQL is deployed.
-- --------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE SEED_RULE_DEFINITION AS
{{ seed_select(policy_seed.rules) }};

EXECUTE IMMEDIATE $$
DECLARE
    rule_drift EXCEPTION (-20002, 'RULE_DEFINITION: a live rule version differs from conf/detection.yml. Register a new rule_version; the seed never overwrites a governed row.');
BEGIN
    LET drifted INTEGER := (
        SELECT COUNT(*)
        FROM SEED_RULE_DEFINITION s
        JOIN RULE_DEFINITION t
          ON t.rule_id = s.rule_id AND t.rule_version = s.rule_version
        WHERE NOT (
                EQUAL_NULL(t.typology_id, s.typology_id)
            AND EQUAL_NULL(TO_JSON(t.clause_ids), TO_JSON(s.clause_ids))
            AND EQUAL_NULL(t.description, s.description)
            AND EQUAL_NULL(t.detection_grain, s.detection_grain)
            AND EQUAL_NULL(t.window_bucket_expr, s.window_bucket_expr)
            AND EQUAL_NULL(t.severity, s.severity)
            AND EQUAL_NULL(t.is_active, s.is_active)
            AND EQUAL_NULL(t.admissible_as_primary_evidence, s.admissible_as_primary_evidence)
        )
    );
    IF (drifted > 0) THEN
        RAISE rule_drift;
    END IF;
    RETURN 'RULE_DEFINITION seed: no drift';
END;
$$;

MERGE INTO RULE_DEFINITION t
USING SEED_RULE_DEFINITION s
   ON t.rule_id = s.rule_id AND t.rule_version = s.rule_version
WHEN NOT MATCHED THEN INSERT (
    rule_id, rule_version, typology_id, clause_ids, description, detection_grain,
    window_bucket_expr, severity, is_active, admissible_as_primary_evidence
) VALUES (
    s.rule_id, s.rule_version, s.typology_id, s.clause_ids, s.description, s.detection_grain,
    s.window_bucket_expr, s.severity, s.is_active, s.admissible_as_primary_evidence
);

-- --------------------------------------------------------------------------
-- REF_CHANNEL -- reference catalogue, upserted
-- --------------------------------------------------------------------------
CREATE OR REPLACE TEMPORARY TABLE SEED_REF_CHANNEL AS
{{ seed_select(policy_seed.channels) }};

MERGE INTO REF_CHANNEL t
USING SEED_REF_CHANNEL s
   ON t.channel_code = s.channel_code
WHEN MATCHED THEN UPDATE SET
    channel_name = s.channel_name,
    is_cash = s.is_cash,
    is_instant = s.is_instant,
    typical_max_inr = s.typical_max_inr
WHEN NOT MATCHED THEN INSERT (channel_code, channel_name, is_cash, is_instant, typical_max_inr)
VALUES (s.channel_code, s.channel_name, s.is_cash, s.is_instant, s.typical_max_inr);
