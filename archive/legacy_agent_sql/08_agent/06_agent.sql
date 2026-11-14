-- ===========================================================================
-- 08_agent / 06_agent.sql
-- ===========================================================================
-- The CREATE AGENT statement that wires all tools together.
--
-- This is the only file that references all the procedures. The agent's
-- orchestration model reads the tool descriptions and decides which to
-- call based on the user's request.
--
-- Flaw 8 fix: all object names come from Jinja template variables.
-- ===========================================================================

USE SCHEMA {{ fq.evidence }};

{% set agent_name = fq.evidence ~ "." ~ cortex.agent %}

CREATE OR REPLACE AGENT {{ agent_name }}
  COMMENT = 'Risk & Compliance Copilot -- case investigation through STR drafting'
  PROFILE = '{"display_name": "Risk Copilot"}'
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
      You are a Risk & Compliance Copilot for an Indian bank.
      You help compliance analysts investigate AML cases and generate
      Suspicious Transaction Report (STR) drafts for FIU-IND under PMLA 2002.

      WORKFLOW (follow this order when asked to generate an STR):
      1. If the user provides a case_id: call collect_case_evidence first
      2. If the user provides raw JSON: call validate_str_inputs first
      3. Call generate_str with the complete evidence JSON
      4. Present the draft STR to the analyst
      5. If asked to review: call review_finding
      6. If asked to route for sign-off: call request_attestation

      RULES:
      1. NEVER fabricate transaction data. Only use what the database or user provides.
      2. Every STR output is a DRAFT. State clearly it requires MLRO review.
      3. Cite specific amounts, dates, and account numbers.
      4. Map suspicion to FIU-IND codes: A=Identity, B=Background, C=Multiple accounts,
         D=Activity in accounts, E=Nature of transactions, F=Value, Z=Other.
      5. When citing amounts, use the pre-computed values from collect_case_evidence,
         not your own arithmetic.
      6. You CANNOT attest or file. Only the MLRO (COMPLIANCE_OFFICER role) can.

    orchestration: |
      When the user provides a case_id, call collect_case_evidence first,
      then pass its output as input_json to generate_str.
      When the user provides raw JSON data, validate it and call generate_str directly.
      When the user asks for a review, call review_finding with the finding_id.
      When the user asks to route for sign-off, call request_attestation with the finding_id.

    sample_questions:
      - question: "Generate an STR for case CASE-4399c4c86db5c6ea"
      - question: "I have a JSON payload for an STR, can you generate it?"
      - question: "Help me create an STR -- walk me through the required fields"
      - question: "Review finding FIND-001 before I send it to the MLRO"
      - question: "Route this finding for MLRO attestation"

  tools:
    - tool_spec:
        type: generic
        name: collect_case_evidence
        description: |
          Reads all signals, transactions, party data, and policy clauses for a case
          from the detection pipeline. Returns assembled JSON ready for STR generation.
          Always call this first when the user provides a case_id.
        input_schema:
          type: object
          properties:
            case_id:
              type: string
              description: "The case_id from CASE_RECORD, e.g. CASE-4399c4c86db5c6ea"
          required:
            - case_id

    - tool_spec:
        type: generic
        name: generate_str
        description: |
          Generates a Suspicious Transaction Report (STR) narrative from evidence JSON.
          Accepts the output of collect_case_evidence or user-provided JSON.
          Returns the formatted STR narrative and metadata. Output is always DRAFT.
        input_schema:
          type: object
          properties:
            input_json:
              type: string
              description: |
                JSON string with STR fields: subject_name, subject_type, accounts[],
                transactions[], suspicion_codes[], suspicion_summary. Can include
                case_id and computed_values from collect_case_evidence.
          required:
            - input_json

    - tool_spec:
        type: generic
        name: validate_str_inputs
        description: |
          Validates STR input fields before generation. Returns which required
          fields are missing and the FIU-IND suspicion code reference.
          Call when you need to check if the user has provided all required fields.
        input_schema:
          type: object
          properties:
            input_json:
              type: string
              description: "JSON string with the current input fields to validate"
          required:
            - input_json

    - tool_spec:
        type: generic
        name: review_finding
        description: |
          Runs adversarial review on a finding: deterministic checks (citation
          resolution, clause validity, numeric accuracy) plus an LLM review by
          a different model family. Returns APPROVE or REJECT with specific defects.
        input_schema:
          type: object
          properties:
            finding_id:
              type: string
              description: "The finding_id from the FINDING table"
          required:
            - finding_id

    - tool_spec:
        type: generic
        name: request_attestation
        description: |
          Creates a formal STR artifact from an approved finding and routes it
          to the MLRO for sign-off. Requires a passing review first.
          The agent CANNOT attest -- only the COMPLIANCE_OFFICER role can.
        input_schema:
          type: object
          properties:
            finding_id:
              type: string
              description: "The finding_id that has a passing review"
          required:
            - finding_id

  tool_resources:
    collect_case_evidence:
      type: procedure
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo.name }}"
        query_timeout: 60
      identifier: "{{ fq.evidence }}.COLLECT_CASE_EVIDENCE"

    generate_str:
      type: procedure
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo.name }}"
        query_timeout: 120
      identifier: "{{ fq.evidence }}.GENERATE_STR"

    validate_str_inputs:
      type: function
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo.name }}"
      identifier: "{{ fq.evidence }}.VALIDATE_STR_INPUTS"

    review_finding:
      type: procedure
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo.name }}"
        query_timeout: 120
      identifier: "{{ fq.evidence }}.REVIEW_FINDING"

    request_attestation:
      type: procedure
      execution_environment:
        type: warehouse
        warehouse: "{{ wh.demo.name }}"
        query_timeout: 60
      identifier: "{{ fq.evidence }}.REQUEST_ATTESTATION"
  $$;
