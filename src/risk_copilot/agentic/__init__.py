"""Cross-account agentic layer for STR generation.

The data (RISK_COPILOT_DEV, Cortex Analyst, the deterministic evidence chain)
lives in one Snowflake account; the Cortex Agent that drafts, reviews and
renders the STR lives in another. The two accounts cannot share data or call
each other (different organisations and regions, trial accounts), so this
package holds one key-pair connection to each and moves a hashed evidence
package between them.

    connections   data_conn() / agent_conn() from bridge/.env
    agent_client  Cortex Agent REST client (agent:run, SSE)
    str_pipeline  generate_ai_str(case_id): end-to-end, used by Slack + Streamlit
"""
