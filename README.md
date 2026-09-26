# Risk & Compliance Copilot

An AML analyst's copilot that carries a detection signal all the way to a
regulator-ready STR filing, built Snowflake-native.

**The governing constraint:** no number in a regulatory filing is ever produced
by a language model. Every figure comes from SQL; the LLM selects, sequences and
narrates.

```
SIGNAL  ->  EVIDENCE  ->  FINDING  ->  ARTIFACT  ->  ATTESTATION
 rules      immutable      LLM        STR filing      human
 fire      row pointers  narrative   (all numbers    Principal
           + result       + citations  from SQL)      Officer
           snapshots
```

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Repo scaffold, layered config, CLI, `doctor`, capability gating | **Done** |
| 1 | Bootstrap + full DDL (28 tables, 7 schemas) | **Done, deployed** |
| 2 | Data synthesis: subgraph sampling, ID maps, reskin, injection, validation gates | Next |
| 3 | Load, dynamic tables, idempotent streams/tasks, replay harness | Pending |
| 4 | Semantic view, Analyst NL→SQL, 8 procedures, policy seeding | Pending — mostly unblocked |
| 5 | ML, evaluation suites, Streamlit, governance, agent object | Pending — partly blocked |

125 unit tests pass; all SQL templates render for both profiles; key-pair auth
works unattended; `deploy --stage bootstrap` is idempotent across runs.

**Account capability is measured, not assumed.** A 98-probe sweep against the
build account (Standard edition, trial, `AWS_AP_SOUTHEAST_7`) produced
`conf/generated/capabilities.ASHWINRAVI004.yml`, which `riskcopilot doctor` reads
to auto-disable unavailable features. That file overrides any prose claim in this
repository.

The entire pipeline critical path works — dynamic tables, `STREAM ON DYNAMIC
TABLE`, tasks, `MERGE`, recursive CTEs, Python stored procedures. So does
**Cortex Analyst** (NL→SQL over semantic views), `AI_AGG`, and all of
`SNOWFLAKE.ML` that this region serves. Blocked: per-row Cortex inference
(`AI_COMPLETE`, 399258), agent *invocation* (399504, though the object deploys),
Cortex Search, External Access Integration, and the Enterprise governance
features (row access policies, masking, DMFs, Time Travel beyond 1 day).

Net effect: **narrative generation is the only genuinely blocked piece of the
copilot.** See
[`docs/enhanced-architecture-diagrams.md` §S2–S3](docs/enhanced-architecture-diagrams.md)
for the capability map, the revised tiering, and why three earlier assumptions
about this account were wrong.

## Quick start

```bash
py -3.12 -m venv .venv
.\.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

cp .env.example .env              # pointers only; no secrets

riskcopilot --profile dev config -s topology   # resolved object names
riskcopilot --profile dev doctor               # preflight
riskcopilot --profile dev deploy --dry-run     # render SQL, no connection
```

### Datasets

The four source datasets total ~950 MB and are **not committed**. They are
declared in [`data/MANIFEST.yml`](data/MANIFEST.yml) with sizes and SHA-256
hashes; `riskcopilot doctor --checksums` verifies them.

| File | Source | Licence |
|---|---|---|
| `data/Kaggle/HI-Small_Trans.csv` | IBM AMLworld | CDLA-Sharing-1.0 |
| `data/Kaggle/HI-Small_accounts.csv` | IBM AMLworld | CDLA-Sharing-1.0 |
| `data/Kaggle/HI-Small_Patterns.txt` | IBM AMLworld | CDLA-Sharing-1.0 |
| `data/targets.simple.csv` | OpenSanctions | CC-BY-NC-4.0 |

## Configuration — three tiers, nothing hardcoded

| Tier | What | Where | Rule |
|---|---|---|---|
| 1 | Credentials | `~/.snowflake/connections.toml`, `cortex secret`, Snowflake `SECRET` | Never in the repo. `conf/` holds only secret *names*. |
| 2 | Topology (db, schema, warehouse, role, lag) | `conf/base.yml` + `conf/profiles/<env>.yml` | No `.sql` file may contain a literal object name; the deployer rejects it. |
| 3 | Business thresholds (₹10L CTR, 3× income, `COUNT>=3`) | `GOVERNANCE.POLICY_CLAUSE` at runtime, seeded from `conf/detection.yml` | Detection SQL joins to the table. A threshold in code is an ungoverned threshold. |

Two profiles coexist in one account: `dev` suffixes every object with `_DEV` and
relaxes target lags, `demo` uses canonical names and full freshness.

```
conf/
  base.yml            topology
  profiles/dev.yml    _DEV suffix, 60-min lags, 500-party sample
  profiles/demo.yml   canonical names, 1-min/5-min lags, 5000-party sample
  generation.yml      seeds, FX rates, injection cohorts, validation gates
  detection.yml       POLICY_CLAUSE seed with provenance per clause
  models.yml          ordered Cortex model candidates per agent mode
  external.yml        MCP/EAI endpoints and secret names
  ml.yml              Tier-1 ML + documented Future Scope
```

Every clause in `conf/detection.yml` must cite a real source document;
`doctor` fails the build otherwise. This is what keeps the demo from citing a
circular number that does not exist.

## Architecture gaps fixed during implementation

The design docs left 17 gaps that would have blocked or silently broken the
build. The load-bearing ones:

| Gap | Problem | Fix |
|---|---|---|
| **02** | L1 generated Faker parties with fresh ids while transactions referenced AMLworld hex accounts. Every join would return nothing — no income (kills `INCOME_ACTIVITY_MISMATCH`), no device (kills `SHARED_DEVICE_CLUSTER`), no region (kills RLS). | AMLworld keys are authoritative. `RAW.ID_MAP_PARTY` / `ID_MAP_ACCOUNT` derive surrogate ids deterministically from source keys; Faker only *dresses* preserved keys. Referential integrity holds by construction. |
| **03** | `RAW_TRANSACTIONS` defined twice — pair-level AMLworld-shaped and leg-level signed INR. | Three tables: `RAW_AMLWORLD_TRANS` (verbatim) → `RAW_TRANSACTIONS` (leg-level, **no label**) + `GROUND_TRUTH.TXN_LABELS`. |
| **04** | Random `declared_annual_income` makes the income rule fire on everyone or no one. | Derived from each party's observed legitimate inflow; the illicit cohort deliberately under-declares. |
| **05** | Random 8% `device_fingerprint` yields **zero** detections — the rule needs shared device **and** fund flow. | Shared fingerprints are co-located with existing flows; 30% are decoys, as false-positive control. |
| **06** | "×83 for INR" across 14 currencies, and FX conversion cannot create ₹9.4–9.8L structuring clusters. | Per-currency rate table + structuring as a separate labelled injection cohort. |
| **08** | Labels are txn-grain, signals are party-window-grain. "F1 > 0.7" had no denominator. | `GROUND_TRUTH.EVAL_GRAIN_PARTY_MONTH` declares party-month as the grain, with `expected_rule_ids` per typology. |
| **09/10** | Rolling-window velocity forces full refresh every 5 min; one non-incremental rule degrades the whole signals DT. | Velocity decomposed into incremental-safe daily aggregate + view; signals split into fast (row-level) and aggregate DTs. |
| **11** | A DT recomputes its full result set, so the stream + task inserts duplicate cases forever. | Deterministic `signal_business_key` (`rule|party|window[|account]`) with a unique constraint; the router MERGEs. Plus a sanctions watermark table. |
| **12** | Reviewer re-verification via Time Travel can't work: `AT(TIMESTAMP=>)` binds per table reference, retention defaults to 1 day, and DTs aren't freely time-travellable. Nor was SHA-256 canonicalisation ever defined. | `EVIDENCE_QUERY_LOG.result_snapshot` + versioned canonical hashing (`risk_copilot.hashing`); Time Travel becomes demonstrable provenance, not the correctness mechanism. |
| **13** | RLS covered 2 DTs; cases/findings were unprotected, and owner's-rights procedures bypass RLS entirely. | `CASE_RECORD.branch_region` denormalised for filtering; procedure rights are an explicit Phase 5 decision. |
| **17** | FIU-IND publishes a *form*, not an XML schema. | Validate against our own JSON-Schema in `REF_FILING_TEMPLATES`, with `schema_authority = INTERNAL_TRANSCRIPTION`. No FINGate conformance claim. |
| **18** | Demo hero party `P-10042 / Rajesh Kumar` was hardcoded but ids are generated — the demo script was fiction. | A selection step picks a party that provably trips every listed rule and pins it to `conf/generated/`. |

Found by tests during implementation: 12-digit surrogate ids are required —
7 digits produced 3 collisions in 5,000 entities and would produce ~24,500
across AMLworld's full population, silently merging distinct parties.

## Snowflake-native ML

**Tier 1** — both are built-in SQL; no `snowflake-ml-python`, no compute pool:

- **`JAROWINKLER_SIMILARITY` fuzzy sanctions screening.** The design *cut* fuzzy
  matching as too complex; it is one scalar function. Exact match raises a
  signal, the fuzzy band raises a review task — never an automatic filing.
- **`SNOWFLAKE.ML.ANOMALY_DETECTION` velocity baselines.** The honest answer to
  "rule-based TMS produce an 80–95% false-positive rate". Unsupervised, so it
  cannot leak labels.

**Future Scope** (documented in `conf/ml.yml` with rationale): Feature Store for
point-in-time-correct training sets, `SNOWFLAKE.ML.CLASSIFICATION` triage
ranking, `TOP_INSIGHTS`, Model Registry, `snowflake.ml.jobs` graph embeddings,
`AI_EMBED` entity resolution.

**Hard boundary:** ML output may rank, prioritise and flag for review. It may
never supply a number that appears in an artifact.
`RULE_DEFINITION.admissible_as_primary_evidence` enforces this in the schema.

## Development

```bash
pytest -q                                  # 101 unit tests, no Snowflake needed
ruff check src tests && ruff format src
mypy src
python -m risk_copilot.pipeline.lint       # render all SQL, both profiles
pre-commit install
```

### Repository layout

```
conf/            layered configuration (see above)
src/risk_copilot/
  config.py      layered loader, fail-fast validation, sql_context contract
  session.py     connections.toml resolution, key-pair auth, error explanation
  ids.py         deterministic identity derivation (GAP-02)
  hashing.py     canonical result hashing (GAP-12)
  doctor.py      preflight diagnostics
  cli.py         single entry point
  pipeline/      idempotent ordered SQL deployer + offline lint
sql/             Jinja SQL templates -- see sql/README.md
tests/unit/      offline tests
data/            MANIFEST.yml; payloads gitignored
docs/            architecture docs and CoCo lifecycle evidence
```

## Known blockers

**Snowflake authentication.** The `ASHWINRAVI004` connection uses
`externalbrowser` and the account returns SAML error **390190** (identity
provider parameter misconfigured — an account-level setting). This blocks
`deploy`, `load`, and verification of `CREATE SEMANTIC VIEW` / `CREATE AGENT`
syntax, model availability and Enterprise ML gating.

Everything offline — config, generation, SQL rendering, unit tests — works. To
unblock, either configure key-pair auth and set
`RC_SNOWFLAKE_PRIVATE_KEY_PATH` (also required for unattended automations), or
have an ACCOUNTADMIN review the SSO security integration.
