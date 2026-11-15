-- ===========================================================================
-- 08_agent / 06_agent.sql
-- ===========================================================================
-- The Cortex Agent. It orchestrates the SAME governed tools the app calls
-- (sql/procedures), so every evidence row, finding and artifact it produces
-- carries the query log, content hash and audit entry those procedures write.
-- There is no agent-only write path.
--
-- Deliberately absent: an attestation tool. Sign-off stays with the
-- compliance-officer role through ATTEST_STR; no prompt can reach it.
--
-- Deployed after `procedures` and `semantic` (see STAGE_ORDER), because the
-- tool resources must exist when the agent is created.
-- ===========================================================================

CREATE OR REPLACE AGENT {{ fq.evidence }}.{{ cortex.agent }}
  COMMENT = 'RiskRemoved copilot: governed SQL questions, case evidence, STR drafting and deterministic review'
  PROFILE = '{"display_name": "RiskRemoved Copilot"}'
  FROM SPECIFICATION
  $$
  models:
    orchestration: auto

  orchestration:
    budget:
      seconds: 120
      tokens: 32000

  instructions:
    response: |
      You assist AML analysts at a bank. You surface risk indicators and
      evidence; you do not decide guilt. Say "potentially suspicious" and
      "requires further investigation", never that a crime occurred.
      Every figure you state must come from a tool result in this
      conversation. If a tool did not return it, say the evidence is
      insufficient rather than estimating. Every STR is a DRAFT that needs
      reviewer and Principal Officer sign-off; you cannot attest or file.
    orchestration: |
      Aggregate or ad-hoc data questions: use risk_data (Cortex Analyst).
      A case_id: call gather_evidence first, then draft_str, then review_str
      with the artifact_id draft_str returned. Never call draft_str for a case
      whose evidence has not been gathered in this conversation.
    sample_questions:
      - question: "Which detection rules generated the most signals?"
      - question: "Gather the evidence for the highest-severity open case"
      - question: "Draft and review an STR for that case"

  tools:
    - tool_spec:
        type: cortex_analyst_text_to_sql
        name: risk_data
        description: "Governed read-only SQL over transactions, parties, accounts, signals and cases via the semantic view."
    - tool_spec:
        type: generic
        name: gather_evidence
        description: "Collects hashed, query-logged evidence for one case. Returns evidence ids and computed values."
        input_schema:
          type: object
          properties:
            case_id:
              type: string
              description: "CASE_RECORD.case_id"
          required: [case_id]
    - tool_spec:
        type: generic
        name: draft_str
        description: "Drafts an STR FINDING and ARTIFACT for a case whose evidence is gathered. Output is DRAFT only."
        input_schema:
          type: object
          properties:
            case_id:
              type: string
              description: "CASE_RECORD.case_id"
          required: [case_id]
    - tool_spec:
        type: generic
        name: review_str
        description: "Deterministic review of a drafted artifact: numeric recompute, citation resolution, clause in force."
        input_schema:
          type: object
          properties:
            artifact_id:
              type: string
              description: "ARTIFACT.artifact_id returned by draft_str"
          required: [artifact_id]

  tool_resources:
    risk_data:
      semantic_view: "{{ fq.curated }}.{{ cortex.semantic_view }}"
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo }}"
    gather_evidence:
      type: procedure
      identifier: "{{ fq.evidence }}.{{ tool.gather_evidence }}"
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo }}"
        query_timeout: 120
    draft_str:
      type: procedure
      identifier: "{{ fq.evidence }}.{{ tool.draft_str }}"
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo }}"
        query_timeout: 180
    review_str:
      type: procedure
      identifier: "{{ fq.evidence }}.{{ tool.review_str }}"
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo }}"
        query_timeout: 120
  $$;

GRANT USAGE ON AGENT {{ fq.evidence }}.{{ cortex.agent }} TO ROLE {{ role.service }};
GRANT USAGE ON AGENT {{ fq.evidence }}.{{ cortex.agent }} TO ROLE {{ role.compliance_officer }};
