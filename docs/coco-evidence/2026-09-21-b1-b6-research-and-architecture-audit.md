# B1–B6 Research, Architecture Audit & Doc Consolidation Session

**Session date:** 2026-09-21
**Session ID:** b22040ca-db2f-4cae-a702-50b8f7816f74
**Transcript:** `../coco-transcripts/2026-09-21-b1-b6-research-and-architecture-audit.json`

## What Was Done

### Pre-Build Research (B1–B6)

| # | Task | Outcome |
|---|---|---|
| B1 | IBM AMLworld HI-Small inspection | 5M txns, 518K accounts, 370 laundering patterns across 8 typologies. Documented schemas, staging DDL, COPY INTO templates, re-skinning transforms. Identified duplicate CSV header issue (positional load required). |
| B2 | OFAC SDN XML verification | Schema reference already in `data/ofac-sdn-schema.md`. Documented XMLGET paths, normalized table layout, ingestion method. |
| B3 | OpenSanctions verification | Downloaded `targets.simple.csv` (80K sanctioned targets, 70 MB) and `entities.ftm.json` (348 MB). Decision: use flat CSV, skip FtM JSON. Documented 16-column schema, staging DDL. OpenSanctions API ruled out — `CORTEX_SEARCH_BATCH` replaces its value. EAI refresh pipeline moved to Future Scope. |
| B4 | STR filing schema validation | Validated Section 24 against actual FIU-IND SBA01–SBA04 form. Found structural mismatch: old doc had 5 sections, real form has 8 parts. Rewrote Section 24 entirely with correct 8-part structure, reason code mapping (A–F/Z), and annexure notes. |
| B5 | Snowflake environment | Partial: account ND28304, AWS_AP_SOUTH_1, ACCOUNTADMIN, COMPUTE_WH. Model names unverified — marked as MUST RESOLVE BEFORE DEV START blocker. |
| B6 | MCP servers | All three confirmed available (Jira, Slack, Google Drive). Mapped to 3 scheduled automations. |

### Architecture Audit

Full deep audit of `enhanced-architecture-diagrams.md` against all project docs. Findings:

- **5 unverified technical claims** identified (model names, Snowpipe Streaming SDK language, CORTEX_SEARCH_BATCH, EXECUTE_AI_EVALUATION, stale Section 24)
- **6 code-level gaps** (no DDL, no DT SQL, no stored proc specs, no semantic view YAML, no agent config, no generator scripts)
- **Scope over-ambition** (24 Tier 1 components, SCD2 complexity, 5 eval suites)
- **Cross-document inconsistencies** (two architecture files, stale SOLUTION-PLAN.md, unmarked Tier 3 items in deep dive)

### Doc Consolidation Actions

| Action | Details |
|---|---|
| Section 24 rewritten | Old 5-section → 8-Part FIU-IND SBA01–SBA04 structure |
| B5 blocker documented | Model names, Cortex Agents, AI functions must be verified before dev |
| Deleted `architecture-diagrams.md` | Superseded by enhanced version |
| Deleted `SOLUTION-PLAN.md` | Consolidated into enhanced architecture doc |
| Added coco-evidence README | Marked as past research, not for development |
| Updated coco-transcripts README | Same — judges only, not developers |
| Simplified replay harness | INSERT loop primary, Snowpipe Streaming → Future Scope |
| SCD2 → Future Scope | Current-state DIM_PARTY only for hackathon |
| Eval suites 3-5 → Future Scope | Keep Suite 1 (Detection F1) + Suite 2 (Text-to-SQL) |
| Tier 1 reduced 24 → 17 | Consolidated DTs, agents, procs into grouped line items |
| Added Future Scope section | 9 items for PPT "What Production Would Need" slide |
| Added Section 29 (Pre-Dev Guidelines) | Specs for DDL, DT SQL, stored procs, semantic view YAML, agent config, generator scripts |

## CoCo Features Demonstrated

- `cortex search docs` — searched for FIU-IND STR requirements
- `cortex search object` — (attempted) Snowflake feature availability
- `sql_execute` — probed account details (CURRENT_ACCOUNT, CURRENT_REGION, SHOW DATABASES, SHOW WAREHOUSES, CORTEX.COMPLETE model tests)
- `cortex conversations search` + `cortex conversations transcript` — session export
- Subagent delegation — parallel Explore agents for doc inventory, general-purpose agents for section reads
- File operations — extensive read/edit/write/delete across project docs
- PowerShell bash — data file analysis (row counts, column inspection, pattern extraction)
