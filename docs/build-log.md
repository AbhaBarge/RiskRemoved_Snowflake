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
| 2 | `patterns.py` + positional ledger reader | **Done** |
| 3 | `graph.py`: seeds, exclusions, BFS, budget, ID maps | **Done** -- illicit-rate calibration open (M18) |
| 4 | `reskin.py` + `labels.py` | **Done** |
| 5 | `entities.py` + `relationships.py` | Pending |
| 6 | `inject.py` + `eval_grain.py` + `demo_entity.py` | Pending |
| 7 | `validate.py`, writer, `riskcopilot generate` | Pending |
| 8 | Run on dev; record measured facts in BUILD STATUS and memory | Pending |

**Verification at the end of step 4:** `pytest -q` 295 passed; ruff, mypy (17 files) and lint clean. Step 4 changed only SQL comments.

**Verification at the end of step 3:** `pytest -q` 269 passed in 37 s; ruff, mypy (15 files), lint and doctor (exit 0, 0 failed) clean.

**Verification at the end of step 2:**

| Check | Result |
|---|---|
| `pytest -q` | 243 passed in 31 s (step 1: 191) |
| `ruff check src tests` | clean |
| `mypy src` (strict) | clean, 14 source files |
| `risk_copilot.pipeline.lint .` | dev + demo, 6 templates each, all render |
| `doctor --profile dev` | exit 0, 0 failed |
| Real-data smoke (readers on the full HI-Small files) | M1, M2, M3, M4, M11 reproduced through the generator's own code; see step 2 below |

Step 1 also passed `deploy --stage ddl` on dev (44 statements, run twice, idempotent). Step 2 changed no SQL.

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
| M14 | Amounts are **not** all 2 dp. Every value with more than 2 places is Bitcoin: 6 dp, on 148,151 received / 146,066 paid values. Non-Bitcoin amounts are always exactly 2 dp. Max integer part: 13 digits | pyarrow refused `decimal128(20,2)` ("rescaling would cause data loss"); fraction-digit counts per currency over all rows | Reader parses at `decimal128(20,6)`, from config. **`RAW_AMLWORLD_TRANS.amount_*` is `NUMBER(20,2)` and would round Bitcoin on COPY** -- see open items |
| M15 | The ledger has 30,470 distinct bank ids after normalisation, none still padded. Accounts ids are never padded; `(bank_id, account_number)` is unique over all 518,581 rows; 166,207 entities, each with exactly one name, and every name has the form `<type> #<n>` | Full-file scans through `source.py` | The reader asserts each of these, so a changed revision fails at read time |
| M16 | `METADATA$FILE_ROW_NUMBER` does **not** count a skipped header: with `SKIP_HEADER = 1` the first data row is 1 | A 3-row CSV on a temporary stage on ASHWINRAVI004, queried with `SKIP_HEADER` 0 and 1 | `src_row_number` = 1-based data-row ordinal, so labels join exactly onto the rows `load` copies into `RAW_AMLWORLD_TRANS` |
| M17 | Accounts per entity are **heavy-tailed**: median 1; 102,689 entities have 1 account, 37,015 have 2-4, 22 have >1,000 (max 7,820). The launderers are the mega-entities: demo's 2,838 surviving seed parties own **96,886** accounts, while the attempts themselves touch only 3,156 | Group-by over the accounts file; seed set via `patterns.py` + `EntityOwnership` | Party-closed inclusion is impossible within a 10,000-account budget. Sampling is **account-level** (D13) |
| M18 | Induced samples are far sparser than `max_transactions` assumes. **dev**: BFS saturates at 2 hops with no budget skip (450 parties / 502 accounts / 9,328 legs, 4 AMLworld illicit legs); 3 hops gives 687 accounts / 12,148 legs; 4 gives 857 / 13,634. **demo**: hits its budgets exactly (5,000 / 10,000) with all 369 surviving attempts captured, but only 66,223 pairs, **4.87% of them illicit before any injection** | `sample_subgraph` on the real files, per profile and hop count | Projected illicit rate vs the 2.50% ceiling. dev: expected 2.85% / worst 4.93% at 2 hops, 2.21% / 3.83% at 3, 1.97% / 3.43% at 4. demo: above the ceiling before injection. `test_dev_cohort_is_sized_for_the_illicit_rate_band` divides by `max_transactions` and is therefore optimistic. **Needs a decision** (open items) |
| M19 | **3,701 of 490,891 legit AMLworld Cash pairs (0.75%)** convert to 90-99.9% of the CTR threshold; 110,458 are at or above it; 108 Cash pairs are illicit | Each Cash credit converted at `fx.rates_to_inr`, then divided by the `CTR_CASH_THRESHOLD` seed and compared with the `STRUCTURING_BAND` seed | Legit in-band cash deposits exist and form a natural false-positive control for `STRUCTURING_CROSS_ACCOUNT`. They must post as DEPOSIT (D17). Whether one host's legit deposits alone ever satisfy the rule is measured after load (D7) |

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
| D13 | 09-26 | Sampling is **account-level**: a party carries only its sampled accounts. Structuring hosts are the exception, seeded with all 2-4 of their accounts | Build | M17. It is also what §S5 #3 specified ("seed from the attempts' accounts"); a bank sees only the accounts it holds |
| D14 | 09-26 | Structuring hosts are selected in `graph.py`, not `inject.py`, and are seeded in **both** seed modes. A host must be INDIVIDUAL, own 2-4 accounts, have no `is_laundering=1` pair anywhere in the ledger, and have at least `host_min_ledger_pairs` eligible pairs | Build | A host outside the sample cannot be injected. A tainted host would carry a second, unrelated label; an inactive one is an island. 12,048 entities qualify |
| D15 | 09-26 | The worst-case injected legs are reserved from `max_transactions` before sampling. An attempt captured only partly is dropped whole (`partial_attempts: drop`). The leg cut ranks legitimate pairs by a hash of their **content**, protecting attempt and host pairs | Build | Keeps the leg budget hard, applies D9's reasoning to sampling truncation, and keeps the sample independent of the ledger's row order |
| D16 | 09-26 | `RAW_TRANSACTIONS.source_ref` is an **opaque** `UTR<16 hex>` token for every leg, source and injected alike, replacing the DDL's documented `amlworld:<n>` / `inject:<cohort>`. `txn_pair_id`s are allocated once, over source and injected pairs together, in hash order | Build | The documented format was a value-level label leak that the column-name gate cannot see: the prefix separates injected rows, the cohort is a label, and `<n>` joins to `RAW_AMLWORLD_TRANS.is_laundering`. Sequential ids would expose injected pairs as the tail. Provenance stays in `TXN_LABELS` |
| D17 | 09-26 | AMLworld Cash pairs post as `WITHDRAWAL` (debit) / `DEPOSIT` (credit) on channel CASH; Credit Card as `PAYMENT`; everything else `TRANSFER` (`txn_types` in config) | Build | M19: legit cash credits land in `STRUCTURING_BAND` and must look like deposits, or the rule's false-positive control is meaningless |

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

### Step 2 -- source layer
- `src/risk_copilot/generation/__init__.py`: the package and `GenerationError`.
- `src/risk_copilot/generation/source.py`:
  - manifest resolution: `dataset()` and `source_dataset()` check presence and declared size; `Dataset.require()` fails when a total the generator asserts is undeclared
  - `read_ledger()`: positional, with the header **asserted**, bank ids normalised through `ids.normalise_bank_id` itself (applied per distinct value, not re-implemented), amounts as `decimal128`, `is_laundering` checked to be in {0, 1}, no nulls, row count checked against the manifest, `src_row_number` prepended (M16)
  - `read_accounts()` + `EntityOwnership`: ownership map with a normalising lookup, accounts per entity, entity prefix, `party_type()` (fails on an unmapped prefix, D10); asserts key uniqueness after normalisation and one name per entity (M15)
- `src/risk_copilot/generation/patterns.py`:
  - strict block grammar: optional `:` detail (M3); BEGIN and END typology must match; unclosed block, nested BEGIN, END without BEGIN, empty attempt, row outside a block, blank line inside a block, wrong field count, non-illicit row, non-plain amount and bad timestamp all fail with `file:line`
  - `attempt_id` is the 1-based block position; each row keeps its line number and raw text for `RAW_LAUNDERING_PATTERNS.txn_data`
  - `match_key()` is the full 10-field identity, built exactly like a ledger row's
  - `seed_accounts()`; totals asserted against `laundering_attempts` / `illicit_transactions` in the manifest
- `src/risk_copilot/config.py`: `data_manifest_file()`, the single path owner; `doctor.py` uses it.
- `conf/generation.yml`: `source.datasets` (manifest key per role) and `source.ledger` (timestamp format, amount precision and scale, M14).
- Fixture `tests/fixtures/amlworld_mini/`: 14 ledger rows, 15 accounts, 3 attempts / 7 rows. It covers:
  - padded bank ids
  - a colon-less STACK header
  - a Bitcoin row (6 dp) inside an attempt
  - Country and Direct entities
  - a cross-currency Wire pair
  - SoleProp hosts with 2-3 accounts
  - one account number at two banks with different owners
  - one unattributed illicit row
  
  The fixture is pinned to LF with `.gitattributes`; CRLF is tested on a converted copy.
- Tests: `tests/unit/test_generation_source.py` (26), `tests/unit/test_generation_patterns.py` (26).
- **Real-data smoke run** through the new readers:
  - patterns: 0.1 s; 370 attempts / 3,209 rows; the 8 typologies with the M3 counts; 3 without detail
  - ledger: 2.2 s, 643 MB in memory; 5,078,345 rows; span 2022-09-01 00:00 to 2022-09-18 16:18; 5,177 illicit
  - accounts: 2.6 s; 518,581 accounts / 166,207 entities
  - **0 unresolved pairs** of 5,078,345 (M1)
  - 3,170 seed accounts owned by 2,851 parties (M11)
  - every one of the 3,209 Patterns rows matches exactly 1 illicit ledger row (M4)

### Step 3 -- subgraph sampling
- `patterns.py`: `locate_attempts()` matches every Patterns row against the **whole** ledger on the full key. It pre-narrows by timestamp + sender, so only a few thousand rows are keyed in Python (0.5 s on the real data). `AttemptLocation` keeps the match counts and any legit-row matches for the `label_attribution` gate rather than asserting them. Real data: `{1: 3209}`, 0 legit matches.
- `src/risk_copilot/generation/graph.py`, `sample_subgraph()`:
  - vectorised exclusions with per-reason counts
  - whole-attempt drop (D9)
  - host selection (D14)
  - seeds by mode
  - account-level BFS (D13): `max_neighbours_per_account` ranked per edge by `stable_hash`, admission in hash order within hard party/account budgets
  - induced pairs, partial-attempt drop and injection reserve (D15)
  - content-ranked leg cut
  - ID maps through `ids.party_id` / `ids.account_id`
  - a `stats` block for the manifest

  Only the implemented option of each policy is accepted (`seed_source` enum, `exclusion_policy`, `partial_attempts`, `require_both_legs_resolvable: true`, an enabled but unimplemented cohort); anything else fails.
- `conf/generation.yml`: `subgraph.partial_attempts`, the account-level inclusion note (M17), `max_transactions` documented as including the injection reserve, `injection.structuring.host_requires_clean_history` / `host_min_ledger_pairs`.
- Fixture: one clean SoleProp host (`800A0000A`, 2 accounts, 2 legit pairs) appended, so no existing row number moved.
- Tests: `tests/unit/test_generation_graph.py` (26). The fixture's BFS is traced by hand in the module docstring and asserted pair by pair. Also covered:
  - exclusion counts per reason
  - whole-attempt drop
  - host qualification
  - both seed modes
  - each hard budget
  - the neighbour cap
  - the leg-cut protection
  - every unsupported setting
  - ID derivation
  - **order independence** (a shuffled ledger samples the same customers and transfer contents)
  - seed sensitivity
- **Real-data runs:**

  | Profile | Time | Parties / accounts | Legs | Attempts captured | Illicit before injection |
  |---|---|---|---|---|---|
  | dev | 19 s | 450 / 502 | 9,328 | 0 (368 not sampled, 1 partial, 1 excluded) | 0.04% of pairs |
  | demo | 21 s | 5,000 / 10,000 | 132,446 | 369 | **4.87%** of pairs |

  Exclusions on the full ledger: 149,019 pairs (146,091 format, 148,204 currency, 110,612 party type; they overlap), 1 attempt. 12,048 entities qualify as hosts.
- **Environment note:** two runs were 25-70x slower than their repeats: a 15.5-minute pytest (36 s on repeat) and a 944 s hop-4 sample (13 s under the profiler). Neither reproduced. Timings in this log are from the fast runs.

### Step 4 -- reskin + labels
- `src/risk_copilot/generation/reskin.py`:
  - `reskin_pairs()`: two legs per pair. Side-specific amounts and currencies; Decimal FX at the configured rounding mode (`fx.rounding_mode`). `retain_foreign_pct` is drawn per pair from a content hash, keeping ISO currency codes (`fx.iso_codes`); `amount_inr` is always set. Channels and `txn_types` come from config (D17); `reference_text` is NULL.
  - Fails on an unknown currency, an unmapped format when `channel_default` is null, a non-negative debit sign, or amounts that would round.
  - `assign_pair_ids()`: one allocation over all pairs in hash order, giving dense, collision-free `TP-` ids plus opaque `source_ref` tokens (D16).
- `src/risk_copilot/generation/labels.py`:
  - `label_source_legs()`: labels from the ledger's `is_laundering`, with attempt, pattern, typology and `match_confidence = 1/n` for captured attempts only
  - fails on an uncatalogued pattern, a ledger row claimed by two attempts, or an attempt row that is not illicit
  - `raw_laundering_patterns()`: all attempts verbatim for `RAW_LAUNDERING_PATTERNS`
- `conf/generation.yml`: `fx.iso_codes`, `fx.rounding_mode`, `txn_types`, `legs.source_ref_prefix` / `source_ref_hex_digits`, `labels.source_injection_method`.
- DDL: comment-only corrections to `RAW_TRANSACTIONS.source_ref` and `INJECTED_TXN_PENDING.source_ref` (D16). No column changed; `CREATE ... IF NOT EXISTS` leaves deployed comments as they were until the next re-create.
- Tests: `tests/unit/test_generation_reskin_labels.py` (22), plus 2 config-coherence tests (source label = AMLworld typologies' `injection_label`; every FX currency has an ISO code).
- **Real data:**

  | Profile | Legs | Reskin | Pairs with a foreign leg | Illicit legs | By typology |
  |---|---|---|---|---|---|
  | dev | 9,328 | 0.5 s | 4.1% | 4 | all unattributed |
  | demo | 132,446 | 7.2 s | 4.9% | 6,446 | all 8 typologies (3,199 attempt pairs) + 24 unattributed pairs |

  `source_ref` is unique in both.

## Departures from the plan

| Plan said | Built | Why |
|---|---|---|
| Add `internal_party_id` / `internal_account_id` to `ids.py` | Not added. The cash desk derives through the existing `party_id("INTERNAL:BANK")` and `account_id("INTERNAL", "CASH_DESK:<region>")` | Same determinism with less surface. The keys cannot collide with AMLworld's (numeric bank ids, hex entity ids; these contain letters and `:`), and `normalise_bank_id` leaves non-numeric ids intact. The combined 1:1 gate still covers collisions |
| `income_rule_yield` gate | `income_calibration`, which reports the generated inflow-to-declared distribution | Counting would-fire parties means applying the rule's threshold in Python, the same objection as D7. The distribution is a property of the data; firing is checked after load |
| "A ledger reader" (module unnamed) | `generation/source.py`, which also holds the accounts reader, the ownership map and manifest resolution | One module owns every positional source contract; `patterns.py` reuses its column tuple and format, so the two can't drift |
| Plan step 7's fixture list included a SoleProp host | Added in step 2 | Costs nothing now, and step 3 needs it |
| Plan left inclusion granularity open; step 3 first built party-closed inclusion | Account-level (D13) | M17: party-closed cannot fit any budget once launderers are seeded |
| Plan puts host ranking in `inject.py` | `graph.py` (D14); `inject.py` consumes `Subgraph.hosts` | Sampling must know the hosts to seed them |

---

## Open items and risks

- **DECISION NEEDED -- illicit-rate calibration (M18).** Both profiles are projected to fail `illicit_rate_pct.max` (2.50%, D6):
  - dev: expected 2.85% at 2 hops, falling to 2.21% at 3 hops and 1.97% at 4
  - demo: 4.87% from AMLworld alone, before any injection

  The levers are hops, cohort size, budgets, or the ceiling itself. The ceiling is a user decision (D6), so it is not changed here. Steps 4-7 do not depend on the answer; the gate measures the real rate in step 7.
- **Demo illicit rate:** the D6 estimate of ~2% assumed a ~400K-leg demo sample; the measured sample is 132K legs and 4.87% illicit before injection (M18). Superseded by the decision item above.
- **Income under-declare factor** is uncalibrated for 18 days (M12). Recalibrate when `INCOME_ACTIVITY_MISMATCH` lands.
- **ML velocity baseline** needs 45 days; the source has 18 (M2). This needs a decision before Phase 5.
- **Dormant reactivation** needs 180 days of dormancy (M2). It needs synthesised history or a redefinition before that cohort is built.
- **Phase 3 obligations created here:**
  - `load` must run after the `policy` seed stage (D2)
  - `DT_PARTY_RELATIONSHIP` must exclude `internal_accounts.src_bank_id` from FUND_FLOW (D4)
  - ground-truth files need a stage the service role cannot read
- **Phase 3 obligation (M14):** `RAW_AMLWORLD_TRANS.amount_received` / `amount_paid` are `NUMBER(20,2)`, so COPY would round Bitcoin's 6 dp values (`0.025852` -> `0.03`) in a table documented as verbatim. Snowflake cannot change a NUMBER column's scale in place, so the fix is `NUMBER(20,6)` in the DDL plus a drop and re-create of the (empty) dev table. That is a destructive step, so it is left for `load` and not applied now. Generated RAW tables are unaffected: Bitcoin is excluded before reskin (D9).
- **Commits:** everything without a credential is committed at the user's standing request, on `feature/ashwinAnalyze`, not pushed (`b8b94b4`, `85c862c`, then one commit per step). Before each commit, the actual secret values from `.env`, `connections.toml` and the key file are compared against every staged file; counts only are printed. `.env` stays gitignored.
- **`docs/enhanced-architecture-diagrams.md` §S4/§S5** still state 0.063% and assume label ambiguity. Corrected in step 8, once the dev run confirms M4/M5 through the generator itself.
