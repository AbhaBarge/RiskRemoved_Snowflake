# Build log -- Phase 2 (data synthesis, thin vertical slice)

Progress record for Phase 2. It covers what is done, what was decided and by
whom, what was measured, and where the build departs from the plan. Kept current
at each step.

- **Plan:** `.snowflake/cortex/plans/phase2-data-synthesis-slice.plan.md`
- **Build truth:** `docs/enhanced-architecture-diagrams.md`, BUILD STATUS (§S1-S6)
- **Evidence:** `conf/generated/capabilities.ASHWINRAVI004.yml`

---

## Status

| Step | Scope | State |
|---|---|---|
| 0 | Pre-work: the two live config bugs (`ml.yml` edition gate, demo retention) | **Done** |
| 1 | Contracts: bank-id normalisation, config corrections, typology seed, `INJECTED_TXN_PENDING` DDL, `.gitignore` | **Done** |
| 2 | `patterns.py` + positional ledger reader | Pending |
| 3 | `graph.py`: seeds, exclusions, BFS, budget, ID maps | Pending |
| 4 | `reskin.py` + `labels.py` | Pending |
| 5 | `entities.py` + `relationships.py` | Pending |
| 6 | `inject.py` + `eval_grain.py` + `demo_entity.py` | Pending |
| 7 | `validate.py`, writer, `riskcopilot generate` | Pending |
| 8 | Run on dev; record measured facts in BUILD STATUS and memory | Pending |

**Verification at the end of step 1:**

| Check | Result |
|---|---|
| `pytest -q` | 191 passed (baseline 125) |
| `ruff check src tests` | clean |
| `mypy src` (strict) | clean |
| `risk_copilot.pipeline.lint .` | dev + demo, 6 templates each, all render |
| `doctor --profile dev` | exit 0, 0 failed |
| `deploy --stage ddl` on dev | 44 statements, ok, run twice, idempotent |

---

## Measured facts

Every fact below was measured against the HI-Small files on 2026-09-26. Each one
contradicted, or was missing from, the original design. The generator
re-asserts them at run time. They are not only recorded here.

| # | Fact | How measured | Consequence |
|---|---|---|---|
| M1 | Bank ids are zero-padded in Trans/Patterns (`010`) and not in accounts (`10`) | Joined 300,000 ledger rows: **0** matched raw, **300,000** matched normalised | `ids.normalise_bank_id()`, applied inside `account_id()` |
| M2 | The ledger spans **2022-09-01 to 2022-09-18 -- 18 days** | Min/max over all 5,078,345 rows | No 12-month income; structuring window 21 -> 14; party-month is one month; 45-day ML baseline and 180-day dormancy are infeasible |
| M3 | **370** attempts, **3,209** rows, **8** typologies. BIPARTITE, SCATTER-GATHER and STACK headers have no `:` | Line counts vs a parser that required `:`, which found only 234 | `patterns.py` must treat the detail suffix as optional |
| M4 | Every Patterns row matches **exactly one** ledger row | Keyed both the narrow (ts, from, to, amount) and the full 10-column tuple over the whole ledger | GAP-09 ambiguity does not occur. `match_confidence` = 1.0, asserted by a gate |
| M5 | The ledger has **5,177** `is_laundering=1` rows; Patterns attribute 3,209 | Count over the ledger | 1,968 illicit rows have no typology. The true base rate is **0.102%**, not 0.063% |
| M6 | 3,208 of 3,209 attributed illicit rows are **ACH**; none are Cash | Format counts in the Patterns file | No AMLworld typology exercises CASH. Structuring can only be injected, and the slice needs no AMLworld typology |
| M7 | `Bitcoin` covers 146,091 pairs, has no FX rate and no channel mapping; 1 illicit row (1 STACK attempt) | Currency/format coverage vs `fx.rates_to_inr` | Excluded, whole pair and whole attempt |
| M8 | `Country #1` holds 6,677 accounts and 75% of Bitcoin flow; `Direct #1/#2` are similar non-customers | Top entities by account count, format mix by entity type | Typed EXTERNAL and excluded. The old default would have made them INDIVIDUAL |
| M9 | Entity mix: Corp 55,530 / SoleProp 55,369 / Partnership 55,170 / **Individual 134** | Accounts file | SoleProp -> INDIVIDUAL (decision D5) |
| M10 | `.gitignore` rule `generated/` also ignored `conf/generated/` | `git check-ignore -v` | The evidence file was never committed. Negated with `!conf/generated/` |
| M11 | The laundering seed set is 3,170 accounts owned by **2,851** parties; dev's budget is 500 | Corrected parser + accounts join | Dev seeds from cohort hosts instead (decision D3) |
| M12 | Income under annualisation: a legit party at median draws sits at ratio **0.057** vs the trigger of 3.0. `ceiling_inr` would falsely flag **9,755** legit-only entities. The 0.35 under-declare factor catches **104 / 3,255** launderers | Legit/illicit inflow per entity, whole ledger | Ceiling removed. Under-declare factor marked uncalibrated until the income rule lands |
| M13 | 37,015 entities have 2-4 accounts; after D5, 12,452 of them are individuals | Accounts file | Enough real hosts for the structuring cohort; no ids minted |

---

## Decisions

| # | Date | Decision | Decided by | Rationale |
|---|---|---|---|---|
| D1 | 09-26 | Annualise the 18-day window for declared income | User | Keeps income a function of the real graph (M2). M12 confirms it does not mass-fire |
| D2 | 09-26 | Structuring amounts are **deferred**: generated as a ratio of the CTR threshold, resolved at load from `V_POLICY_CLAUSE_CURRENT` | User (proposed it) | The threshold then genuinely comes from POLICY_CLAUSE at runtime while generation stays offline. The ratio is kept out of RAW to avoid a label leak |
| D3 | 09-26 | Dev uses slice-only seeding (`seed_source: injection_hosts`); broaden later | User | No subsampling code; the slice's rule needs no AMLworld typology (M6) |
| D4 | 09-26 | The debit side of a cash deposit is a **branch cash-desk account** | User | How a core banking system posts it. A narrow, documented exception to "never mint ids" (below) |
| D5 | 09-26 | Sole Proprietorship -> INDIVIDUAL | User | RBI KYC does CDD on the proprietor as an individual; otherwise 99.9% of the base is companies (M9) |
| D6 | 09-26 | `illicit_rate_pct.max` 1.50 -> **2.50** | User | Seeding demo from launderers concentrates illicit legs at ~2%; accepted |
| D7 | 09-26 | `signal_yield` is evaluated **after load in Snowflake**, not faked offline | User | Re-implementing rules in Python would put detection logic outside Snowflake |
| D8 | 09-26 | Structuring `window_days` 21 -> 14 | Build | Must fit the 18-day ledger (M2) and `STRUCTURING_WINDOW_DAYS` |
| D9 | 09-26 | Bitcoin and EXTERNAL entities excluded, whole pair + whole attempt | Build | M7, M8. Costs 1 of 370 attempts |
| D10 | 09-26 | `party_type_default: null`, so an unmapped entity prefix fails generation | Build | Guessing INDIVIDUAL is what mis-typed `Country #1` |
| D11 | 09-26 | `rapid_credit_velocity` and `dormant_reactivation` set `enabled: false` | Build | Not in the slice; dormancy is infeasible as configured (M2) |
| D12 | 09-26 | Graph typologies carry `expected_rule_ids: []` | Build | Which rule catches a STACK is to be measured, not declared in advance |

---

## Changes, by step

### Step 0 -- live config bugs
- `conf/ml.yml`:
  - `require_edition: []`, citing probe evidence
  - `velocity_anomaly_ml`, `feature_store` and `ml_model_provenance` now OK in doctor
  - CLASSIFICATION / TOP_INSIGHTS recorded as regional (`a_absent`)
  - Feature Store / Registry deferral reworded as scope, not capability
  - The 45-day baseline contradiction (M2) is left visible, not silently edited
- `src/risk_copilot/config.py`:
  - retention is clamped from the recorded evidence; the cap is parsed from the account's own 001048 message, with the account parameter as fallback
  - `Settings.retention_clamp` records what was reduced and why
  - `capabilities_file()` is the single path owner
- `doctor.py` reports the clamp as WARN.
- Result: demo renders `DATA_RETENTION_TIME_IN_DAYS = 1` instead of 14, which would have failed a deploy.
- Tests: `tests/unit/test_retention_clamp.py`.

### Step 1 -- contracts
- `src/risk_copilot/ids.py`: `normalise_bank_id()`. It strips zero padding from numeric ids only and is applied inside `account_id()`.
- `conf/generation.yml`:
  - new `source` exclusions
  - `seed_source` enum; `max_transactions` documented as legs
  - entity type map (D5, M8, D10)
  - income block (D1, M12)
  - `internal_accounts` (D4)
  - structuring block (D2, D8, clause ids, `amount_ctr_ratio_decimals`)
  - new gates; wider `forbidden_columns`; D6; D7 note
- `conf/profiles/dev.yml`: `seed_source: injection_hosts`, `cohort_parties: 10`, dev `must_trigger_rules`, corrected comments.
- `conf/detection.yml`: `typologies` seed, with 3 injected and 8 AMLworld entries.
- `sql/01_ddl/04_audit_and_ground_truth.sql`: `GROUND_TRUTH.INJECTED_TXN_PENDING`.
- `.gitignore`: `!conf/generated/` (M10).
- Tests:
  - `tests/unit/test_ids.py`: normalisation
  - `tests/unit/test_generation_config.py`: injection/detection coherence, typology coverage, entity typing, profile seeding, dev cohort sizing, DDL label isolation, plus a negative control proving the leakage parser is not vacuous

---

## Departures from the plan

| Plan said | Built | Why |
|---|---|---|
| Add `internal_party_id` / `internal_account_id` to `ids.py` | Not added. The cash desk derives through the existing `party_id("INTERNAL:BANK")` and `account_id("INTERNAL", "CASH_DESK:<region>")` | Same determinism with less surface. The keys cannot collide with AMLworld's (numeric bank ids, hex entity ids; these contain letters and `:`), and `normalise_bank_id` leaves non-numeric ids intact. The combined 1:1 gate still covers collisions |
| `income_rule_yield` gate | `income_calibration`, which reports the generated inflow-to-declared distribution | Counting would-fire parties means applying the rule's threshold in Python, the same objection as D7. The distribution is a property of the data; firing is checked after load |

---

## Open items and risks

- **Demo illicit rate** is expected around 2%, inside the new 2.50% ceiling (D6). It is measured when demo first runs.
- **Income under-declare factor** is uncalibrated for 18 days (M12). Recalibrate when `INCOME_ACTIVITY_MISMATCH` lands.
- **ML velocity baseline** needs 45 days; the source has 18 (M2). This needs a decision before Phase 5.
- **Dormant reactivation** needs 180 days of dormancy (M2). It needs synthesised history or a redefinition before that cohort is built.
- **Phase 3 obligations created here:**
  - `load` must run after the `policy` seed stage (D2)
  - `DT_PARTY_RELATIONSHIP` must exclude `internal_accounts.src_bank_id` from FUND_FLOW (D4)
  - ground-truth files need a stage the service role cannot read
- **Commit pending:** `conf/generated/capabilities.ASHWINRAVI004.yml` is now trackable but **not committed**. It holds the account locator, user name and credit balance, and no credentials. It is committed only on request.
- **`docs/enhanced-architecture-diagrams.md` §S4/§S5** still state 0.063% and assume label ambiguity. Corrected in step 8, once the dev run confirms M4/M5 through the generator itself.
