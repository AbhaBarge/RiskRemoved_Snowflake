# SQL templates — the `sql_context` contract

Every file under `sql/` is a **Jinja2 template**, not executable SQL. It is
rendered by `risk_copilot.pipeline.deploy.Deployer` against
`Settings.sql_context()` and applied in the fixed order below.

## Why templates

A literal object name in a `.sql` file means the profile suffix stops applying,
so a `dev` deploy silently overwrites `demo` objects in the same account.
`Deployer.render()` therefore **rejects** any file containing
`RISK_COPILOT`, `RC_*_WH`, `RC_ADMIN`, `RC_SERVICE_ROLE`, `COMPLIANCE_OFFICER`
or `BRANCH_ANALYST_*` as a literal.

## Available variables

| Expression | Renders to (dev profile) |
|---|---|
| `{{ profile }}` | `dev` |
| `{{ suffix }}` | `_DEV` |
| `{{ db }}` | `RISK_COPILOT_DEV` |
| `{{ schema.raw }}` | `RAW` |
| `{{ fq.raw }}` | `RISK_COPILOT_DEV.RAW` |
| `{{ fq.curated }}` `{{ fq.governance }}` `{{ fq.evidence }}` `{{ fq.audit }}` `{{ fq.ground_truth }}` `{{ fq.ml }}` | fully qualified schema |
| `{{ wh.dev }}` `{{ wh.pipeline }}` `{{ wh.demo }}` | `RC_PIPELINE_WH_DEV` |
| `{{ wh_spec.pipeline.size }}` | `XSMALL` |
| `{{ role.admin }}` `{{ role.service }}` `{{ role.compliance_officer }}` | `RC_ADMIN_DEV` |
| `{{ role.branch_analysts.MUMBAI }}` | `BRANCH_ANALYST_MUMBAI_DEV` |
| `{{ stage.data }}` `{{ stage.policy_docs }}` `{{ stage.generated }}` `{{ stage.scripts }}` `{{ stage.ground_truth }}` | stage name (`ground_truth` lives in `{{ fq.ground_truth }}` and is granted to no role) |
| `{{ retention.landing_days }}` | `3` |
| `{{ dt.signals_fast.name }}` / `.target_lag` | `DT_SIGNALS_FAST_DEV` / `60 minutes` |
| `{{ view.txn_velocity_features }}` | `V_TXN_VELOCITY_FEATURES_DEV` |
| `{{ cortex.semantic_view }}` `{{ cortex.search_service }}` `{{ cortex.agent }}` | Cortex object names |
| `{{ ml.tier1.* }}` | structural ML settings from `conf/ml.yml` |
| `{{ policy_seed.clauses }}` `.typologies` `.rules` `.channels` | `conf/detection.yml` as rows of `{column: typed SQL literal}` (`src/risk_copilot/policy.py`). **Only `05_policy` may use it**: it is how threshold values enter Snowflake, as POLICY_CLAUSE rows. A test fails if any other stage renders a threshold |
| `{{ rule.<RULE_ID>.id }}` `.version` `.clause.<role>` `.anchor` `.dynamic_table` | Per registered rule, for `sql/rules/<RULE_ID>.sql`: the rule id and each role's clause id as literals, the window anchor as a DATE literal. Names only -- the SQL joins each clause id to `V_POLICY_CLAUSE_CURRENT` for its value |
| `{{ internal.bank_id }}` | `internal_accounts.src_bank_id` as a literal: identifies the bank's own (cash desk) accounts in CURATED |

**`sql/rules/`** holds one detection SELECT per registered rule, included by the
signal dynamic table its `dynamic_table` names. Each file is one SELECT (no `;`),
linted like a stage file, and hashed into `RULE_DEFINITION.rule_sql_hash`: after
the first deploy, editing it fails the policy stage until the rule gets a new
`rule_version`. A registered rule without a file fails the render.

`StrictUndefined` is enabled: a typo raises at render time instead of emitting an
empty identifier and creating the wrong object.

## Rules

1. **No literal object names.** Use the table above.
2. **No thresholds.** Join `{{ fq.governance }}.V_POLICY_CLAUSE_CURRENT` instead
   of writing `1000000`. A number embedded in detection SQL is a hardcoded
   regulatory threshold, which is the thing `POLICY_CLAUSE` exists to prevent.
3. **Idempotent statements only** — `CREATE OR ALTER`, `CREATE IF NOT EXISTS`,
   `CREATE OR REPLACE VIEW`. Enforced for `bootstrap` and `ddl` by
   `tests/unit/test_deploy.py`.
4. **No adjacent string literals.** Snowflake does not concatenate
   `'part one '` / `'part two'` on consecutive lines the way C does — it is a
   syntax error. Keep a literal on one line or join with `||`. Linted.
5. **One statement per semicolon.** Dollar-quoted procedure bodies are masked
   before splitting, so semicolons inside `$$ ... $$` are safe.

## Stage order

| # | Stage key | Directory | Contents |
|---|---|---|---|
| 1 | `bootstrap` | `00_bootstrap` | database, schemas, warehouses, roles, grants, stages, file formats |
| 2 | `ddl` | `01_ddl` | all tables and base views |
| 3 | `dynamic_tables` | `02_dynamic_tables` | the DT graph |
| 4 | `streams_tasks` | `03_streams_tasks` | `SIGNALS_STREAM`, `ROUTE_SIGNALS` |
| 5 | `governance` | `04_governance` | row access + masking policies, DMFs |
| 6 | `policy` | `05_policy` | policy seed, `AI_PARSE_DOCUMENT` ingestion |
| 7 | `search` | `06_search` | Cortex Search service |
| 8 | `semantic` | `07_semantic` | semantic view |
| 9 | `agent` | `08_agent` | Cortex Agent |
| 10 | `ml` | `09_ml` | fuzzy sanctions matching, anomaly detection |
| 11 | `automations` | `10_automations` | scheduled tasks |
| 12 | `procedures` | `procedures` | the 8 agent tool procedures |

Stages always apply in this order regardless of the order of `--stage` flags, so
a mis-ordered command line cannot produce a broken deploy. Execution stops at the
first failing file, because later stages depend on earlier ones.

## Usage

```bash
riskcopilot --profile dev deploy --dry-run              # render only
riskcopilot --profile dev deploy --out-dir build/sql    # inspect rendered SQL
riskcopilot --profile dev deploy --stage bootstrap --stage ddl
riskcopilot --profile demo deploy                       # all stages
```
