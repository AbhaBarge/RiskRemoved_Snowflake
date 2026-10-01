-- ===========================================================================
-- procedures / 01_case_tools_common.sql
-- ===========================================================================
-- What the case tools share: the STR filing template and two small UDFs.
--
-- The template is governed reference data (REF_FILING_TEMPLATES), insert-only:
-- changing the narrative or the required fields is a new template_version,
-- never an edit, so a filed STR still names the exact template it was built
-- from. FIU-IND publishes a form, not a schema; this is our transcription
-- (schema_authority INTERNAL_TRANSCRIPTION) and no FINGate conformance is claimed.
--
-- The narrative holds {placeholders} only, never a figure. DRAFT_STR fills them
-- from FINDING.computed_values, each tied to the EVIDENCE_QUERY_LOG row that
-- produced it -- "no number in a filing comes from a language model". While
-- this account has no AI_COMPLETE the prose itself is this fixed template.
-- ===========================================================================

MERGE INTO {{ fq.governance }}.REF_FILING_TEMPLATES t
USING (
    SELECT
        '{{ tool.str_template_id }}' AS template_id,
        'STR' AS artifact_type,
        '1' AS template_version,
        'FIU-IND' AS regulator,
        'Suspicious Transaction Report (STR), FIU-IND reporting format' AS source_form,
        OBJECT_CONSTRUCT(
            'required_fields', ARRAY_CONSTRUCT('report_type', 'case_id', 'finding_id', 'subject', 'transactions', 'typology_id', 'grounds_of_suspicion', 'cited_clause_ids', 'suspicion_established_at', 'filing_deadline'),
            'narrative_template', 'Between {first_deposit_at} and {last_deposit_at}, {party_name} ({party_id}, {occupation}) received {deposit_count} cash deposits totalling {deposit_total_inr} into {account_count} different accounts at the {branch_region} branch. Every deposit was between {min_deposit_inr} and {max_deposit_inr}, just below the cash transaction reporting threshold of {ctr_threshold_inr}. Splitting cash into repeated deposits just under the reporting threshold, across several accounts, is consistent with structuring to avoid cash transaction reporting.'
        ) AS field_definitions,
        'INTERNAL_TRANSCRIPTION' AS schema_authority,
        TRUE AS is_active
) s
ON t.template_id = s.template_id AND t.template_version = s.template_version
WHEN NOT MATCHED THEN INSERT (
    template_id, artifact_type, template_version, regulator, source_form,
    field_definitions, schema_authority, is_active
) VALUES (
    s.template_id, s.artifact_type, s.template_version, s.regulator, s.source_form,
    s.field_definitions, s.schema_authority, s.is_active
);

-- Indian digit grouping (12,34,567), whole rupees. Display only: every
-- comparison the Reviewer makes is on the numeric `value`, never this string.
CREATE OR REPLACE FUNCTION {{ fq.evidence }}.{{ tool.format_inr }}(AMOUNT FLOAT)
    RETURNS VARCHAR
    LANGUAGE JAVASCRIPT
    COMMENT = 'Formats rupees with Indian digit grouping, for display in a filing.'
AS
$$
if (AMOUNT === null || AMOUNT === undefined) { return null; }
var neg = AMOUNT < 0;
var s = Math.round(Math.abs(AMOUNT)).toString();
var last3 = s.slice(-3);
var rest = s.slice(0, -3);
if (rest.length > 0) { rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + ","; }
return (neg ? "-" : "") + "\u20B9" + rest + last3;
$$;

-- {placeholder} -> computed_values[placeholder].display. An unknown
-- placeholder is left as written, so the Reviewer's check can see it.
CREATE OR REPLACE FUNCTION {{ fq.evidence }}.{{ tool.render_template }}(TEMPLATE VARCHAR, VALS VARIANT)
    RETURNS VARCHAR
    LANGUAGE JAVASCRIPT
    COMMENT = 'Fills {placeholders} from FINDING.computed_values display strings.'
AS
$$
if (TEMPLATE === null) { return null; }
return TEMPLATE.replace(/\{([a-z_]+)\}/g, function (m, k) {
    var e = VALS ? VALS[k] : null;
    return (e && e.display !== undefined && e.display !== null) ? String(e.display) : m;
});
$$;
