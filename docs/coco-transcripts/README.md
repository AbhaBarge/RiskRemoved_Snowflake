# CoCo CLI Raw Transcripts — Evidence of Tool Usage

> **FOR HACKATHON JUDGES ONLY. DO NOT READ DURING DEVELOPMENT.**
> These files are past research and planning transcripts preserved as CoCo lifecycle evidence (Phase 1: Planning). They contain no build instructions. The single source of truth for development is `docs/enhanced-architecture-diagrams.md`.

> These are raw JSON exports of CoCo CLI (Cortex Code) sessions, exported via
> `cortex conversations transcript <id> --output json`. They contain every user
> message, every assistant response, and every tool call (with inputs and outputs)
> made during the planning and development of the Risk & Compliance Copilot.

## Why these exist

The hackathon judging criteria require evidence that CoCo CLI was used across
planning, development, execution, and testing. The summary documents in
`docs/coco-evidence/` are human-readable but do not show the actual tool
invocations. These raw transcripts do.

## What constitutes proof of CoCo CLI usage

In each JSON file, look for `tool_use` blocks which show:
- `cortex search docs` — Snowflake documentation research
- `cortex search marketplace` — Marketplace listing discovery
- `sql_execute` — SQL execution and validation against Snowflake
- `cortex search object` — Snowflake catalog object discovery
- `cortex semantic-views` — Semantic view operations
- `cortex lineage` — Lineage analysis
- `bash` tool calls running `cortex` CLI commands
- `read`, `write`, `edit` — file operations in the repo
- `task` — subagent delegation for parallel research
- `glob`, `grep` — codebase search

## Session index

| File | Session ID | Date | Title | Messages | Key CoCo CLI usage demonstrated |
|---|---|---|---|---|---|
| `01-building-risk-compliance-copilot-392b3fc1.json` | 392b3fc1 | 2026-09-19 | Building a Risk and Compliance Copilot for Banking | 16 | Domain research, target user analysis, process mapping, `cortex search docs` for Snowflake capabilities |
| `02-building-risk-compliance-copilot-e8ccbb47.json` | e8ccbb47 | 2026-09-19 | Building a Risk and Compliance Copilot for Banking | 16 | Current-day banking systems analysis, gap analysis, cross-silo bridge design |
| `03-data-sources-and-diagrams-c40a0959.json` | c40a0959 | 2026-09-21 | Data Sources, Architecture Diagrams | 38+ | `cortex search marketplace` (7 searches: sanctions, PEP, AML, KYC, fraud, credit risk, adverse media), `cortex search docs` (Snowpipe Streaming, dynamic tables, AI_PARSE_DOCUMENT), subagent delegation for parallel Marketplace research, Mermaid diagram generation, `cortex conversations transcript` for self-export |
| `04-dbt-lineage-b48f8e68.json` | b48f8e68 | 2026-09-18 | Understanding Upstream Lineage in dbt Models | 50 | `cortex lineage`, dbt model exploration, codebase analysis |
| `2026-09-21-b1-b6-research-and-architecture-audit.json` | b22040ca | 2026-09-21 | B1–B6 Research, Architecture Audit & Doc Consolidation | 128+ | `sql_execute` (account probing, model testing), `cortex search docs`, `cortex conversations search/transcript`, subagent delegation (Explore + general-purpose), extensive file read/edit/write/delete, PowerShell data analysis (CSV row counts, pattern extraction) |
| `2026-09-23-snowflake-keypair-auth-fix.json` | 810ca580 | 2026-09-23 | Snowflake Key-Pair Auth Fix & Bootstrap Deploy | 200 | Live debugging of Snowflake error 390190: connector probes to isolate account- vs local-level fault, RSA key-pair generation and `ALTER USER ... SET RSA_PUBLIC_KEY`, authentication-policy and edition discovery, `AI_COMPLETE` trial-account diagnosis, `read`/`edit`/`write` fixes to `session.py`/`doctor.py`/`deploy.py` (SQL statement splitter), `pytest`/`ruff`/`mypy` verification, successful `deploy --stage bootstrap` |
| `2026-09-23-snowflake-capability-probe-sweep.json` | e4e54de3 | 2026-09-23 | Snowflake Account Capability Probe Sweep | 264 | **98 capability probes** across Cortex AI, governance, ML and the pipeline critical path on a Standard trial account. `sql_execute` probes with failure-class triage (edition vs trial vs region vs privilege), Cortex Analyst REST invocation (HTTP 200, NL→SQL confirmed), 28-model `AI_COMPLETE` roster sweep, `CREATE`/`DROP` lifecycle tests for row access + masking + DMF + Cortex Search + agent, `cortex search docs` for model availability, throwaway probe database with verified teardown, new `capabilities.py` + evidence file, `pytest`/`ruff`/`mypy` verification |
| `2026-09-23-phase0-1-scaffold-config-and-ddl.json` | e5423585 | 2026-09-23 | Phase 0–1: Architecture Audit, Scaffold, Config & DDL | 210 | Gap audit of the architecture docs (17 defects found pre-code), Plan Mode → approved plan, three-tier config layer authored, `risk_copilot` package (`config`/`session`/`ids`/`hashing`/`doctor`/`cli`/`pipeline`), 28-table DDL as Jinja templates, idempotent SQL deployer + offline lint, 105 unit tests written and run, `ruff`/`mypy --strict` clean, `$cortex-code-guide` skill invocation, `cortex connections list`, `cortex conversations list/transcript` for self-export, work-split and parallelisation analysis |

> **Credential hygiene:** `2026-09-23-snowflake-keypair-auth-fix.json` is redacted.
> The session printed `~/.snowflake/connections.toml` while diagnosing the auth
> failure, so the OAuth token it contained is replaced with
> `[REDACTED-CREDENTIAL]`. Verified against every secret on disk before commit.
> Apply the same check to any future transcript that inspects a credential file.
>
> The two 2026-09-23 transcripts added after the auth fix were scanned before
> commit for PEM headers (`-----BEGIN`), JWTs (`eyJ...`), long base64 blobs, and
> assignment-form `token=` / `password=` / `passphrase=` patterns. **No
> credential material found.** The long `CAIS...` base64 strings present in both
> are model thinking-block signatures, not secrets. Re-run this scan on any new
> transcript:
>
> ```powershell
> $c = Get-Content <file>.json -Raw
> ([regex]::Matches($c,'-----BEGIN')).Count                      # expect 0
> ([regex]::Matches($c,'eyJ[A-Za-z0-9_-]{20,}')).Count           # expect 0
> [regex]::Matches($c,'(token|password|passphrase)\s*=\s*"[^"]{4,}')  # expect none
> ```


## How to read the transcripts

Each JSON file contains an array of message objects. The key fields:
- `role`: "user" or "assistant"
- `content`: array of content blocks, each with a `type`:
  - `"text"` — human-readable text
  - `"tool_use"` — a CoCo CLI tool invocation (name, input parameters)
  - `"tool_result"` — the output returned by the tool

To find all tool calls in a transcript:
```powershell
# PowerShell — count tool calls per transcript
Get-Content "01-building-risk-compliance-copilot-392b3fc1.json" | 
  Select-String '"type":\s*"tool_use"' | 
  Measure-Object
```

## Relationship to other evidence

| Evidence type | Location | Shows |
|---|---|---|
| Raw transcripts | `docs/coco-transcripts/*.json` | Every tool call, every command, full conversation flow |
| Human-readable summaries | `docs/coco-evidence/*.md` | Distilled analysis, decisions, and reasoning |
| Architecture diagrams | `docs/architecture-diagrams.md` | Visual output produced by CoCo CLI sessions |
| Solution plan | `docs/SOLUTION-PLAN.md` | Master plan document, authored and revised in CoCo CLI |
