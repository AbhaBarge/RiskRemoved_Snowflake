# Build log -- Phase 2 (data synthesis) and Phase 3 (load, pipeline, replay)

Progress record for Phases 2 and 3. It covers what is done, what was decided and
by whom, what was measured, and where the build departs from the plan. Kept
current at each step. Phase 3 has its own status table and step log below the
Phase 2 sections; measured facts and decisions are numbered continuously.

- **Plans:** `.snowflake/cortex/plans/phase2-data-synthesis-slice.plan.md`, `.snowflake/cortex/plans/phase3-load-dt-replay.plan.md`
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
| 5 | `entities.py` + `relationships.py` | **Done** |
| 6 | `inject.py` + `eval_grain.py` + `demo_entity.py` | **Done** |
| 7 | `validate.py`, writer, `riskcopilot generate` | **Done** |
| 8 | Run on dev; record measured facts in BUILD STATUS and memory | **Done**. dev passes; the two open decisions were made (D25, D26), and demo passes every data gate at 2.06% |

**Verification at the end of step 7:**

| Check | Result |
|---|---|
| `pytest -q` | 365 passed in 52 s |
| `ruff check src tests` | clean |
| `mypy src` (strict) | clean, 26 source files |
| `risk_copilot.pipeline.lint .` | dev + demo, 6 templates each, all render |
| `doctor --profile dev` | exit 0, 0 failed |
| `deploy --stage ddl` on dev | every file ok, run twice, idempotent (steps 4-7 changed DDL comments only) |
| `riskcopilot --profile dev generate` on the real files | **exit 0, all blocking gates pass, 18 s** |
| Determinism | two dev runs: all 12 files and the demo pin byte-identical (SHA-256) |

**Verification at the end of step 6:** `pytest -q` 333 passed in 46 s; ruff, mypy (23 files) clean.

**Verification at the end of step 5:** `pytest -q` 317 passed in 43 s; ruff, mypy (20 files) clean.

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
| M20 | Faker `hi_IN` writes names and companies in **Devanagari** (`हर्शद कुमारजी`) and formats phones as `+91 0433218196` (leading 0, not a mobile). `en_IN` gives Latin-script Indian names with the same per-seed determinism (Faker 40.39.0) | Seeded provider calls on both locales | Locale switched to `en_IN`; phones generated from a configured mobile format. Devanagari names would make JAROWINKLER screening against the Latin-script OpenSanctions list meaningless |
| M21 | **Legitimate activity ends on 2022-09-10.** 99.98% of the 5,078,345 pairs fall on 09-01..09-10; 09-11..09-18 holds 1,108 pairs, **655 illicit (59%)**. 109 of 370 attempts (29%) run into the tail. dev's sample has no pair after 09-10; demo's has 452 legit / 652 illicit pairs there | Pair counts per calendar day over the ledger; per-attempt last-row day; the same over each profile's sample | The usable legit window is **10 days**, not 18. The income span is measured over legit legs (D19) and the structuring window goes 14 -> 7 (D18). The tail is itself a temporal label proxy inherited from AMLworld; cut at 09-10 (D25) |
| M22 | With the 09-10 cut, demo's illicit rate falls **5.44% -> 3.06%** (all 261 surviving attempts). Seeding N attempts per typology: 40 -> 3.03%, 30 -> 2.78%, **25 -> 2.51%**, **20 -> 2.06%**, 15 -> 1.89%. At 20, every typology keeps exactly 20 attempts (160 captured) | Full sample -> reskin -> entities -> inject on demo, per N | N = 20 (D26) |
| M23 | **The cash desk was a label proxy.** On the `b450d3a` dev output, all **196 of 196** injected legs touch a cash-desk account and **0 of 9,862** source legs do: after load, "counterparty is the cash desk" would select exactly the planted rows. Neither `label_leakage` scan can see it, because the desk exists only in the ID maps | Desk account ids from `ID_MAP_ACCOUNT` (`src_bank_id = INTERNAL`) against both leg files | D27 |
| M24 | **Booking both halves of a cash pair leaves a twin.** With payer -> desk + desk -> payee (the first D27 build), **598/598** dev and **7,822/7,822** demo genuine deposits have a same-minute, equal-INR customer withdrawal; **0/98** and **0/405** planted deposits do -- nor does any planted amount equal *any* withdrawal in the window (0/98, 0/405), so moving the withdrawal in time would not help | Self-join of desk deposits on (timestamp, amount) against customer debits, both profiles | D30 |
| M25 | **The fixed branch-hours window was a tell.** AMLworld spreads cash over all 24 hours: **25.6%** (dev) / **24.2%** (demo) of genuine desk deposits fall in 10:00-16:00, against **100%** of planted ones under D22. With times drawn from the genuine deposits: planted **28.7%** / **23.2%**, and 23 / 24 distinct hours | Hour histogram of desk-deposit credit legs, before and after D29 | D29 |
| M26 | **A retained foreign currency marked genuine cash deposits.** `fx.retain_foreign_pct` kept a foreign currency on **35 of 598** dev / **338 of 7,822** demo genuine desk deposits; no planted deposit is ever foreign. After D31: 0 / 0 (all legs: 3.61% / 4.49% foreign) | Currency of desk-deposit credit legs | D31 |

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
| D18 | 09-26 | Structuring `window_days` 14 -> **7**; deposits are placed inside the measured legit window | Build | M21. 14 days does not fit a 10-day legit window, and injected deposits in the illicit tail would be recognisable by timing alone |
| D19 | 09-26 | Income annualisation (D1) and all date anchors use the span of **legitimate** sampled legs | Build | M21. Measured over all legs, demo's illicit tail stretches the span to 18 days and understates legit income by 10/18 |
| D20 | 09-26 | A host needs at least `accounts_per_party_min` **deposit** accounts (`deposit_account_types: [SAVINGS, CURRENT]`). Account type is a pure function of the account id (`draws.account_type`), shared by sampler and entity builder | Build | A cash deposit into a LOAN or FD account is not a structuring pattern a bank would recognise |
| D21 | 09-26 | Faker locale `hi_IN` -> `en_IN`; Indian mobile numbers from a configured format | Build | M20 |
| D22 | 09-26 | Structuring deposits fall in branch hours (`deposit_hours: [10, 16]`, minute resolution); one `window_days` burst per host at a hashed offset inside the legit window | Build | Deposits at arbitrary clock times or in the illicit tail would be recognisable without any rule. **Clock part superseded by D29** (M25: the window was itself the tell) |
| D23 | 09-26 | `EVAL_GRAIN_PARTY_MONTH.typology_id` = the month's dominant typology (most illicit legs, smallest id on ties); `expected_rule_ids` = the **union** over every typology present | Build | One party can be in two typologies; recall for each rule must count it |
| D24 | 09-26 | The demo hero must have been **constructed** to trip every `must_trigger_rules` entry (its eval grain expects them all). Otherwise `generate` fails (`fail_if_not_found`) | Build | Demo lists RAPID_CREDIT_VELOCITY and SHARED_DEVICE_CLUSTER, whose cohorts do not exist yet; pinning a party that cannot show the story would repeat GAP-18 |
| D25 | 09-27 | **The ledger is cut at 2022-09-10** (`source.window_end`). Later pairs are excluded, and every attempt reaching past the cut is dropped whole (109 of 370) | **User** | M21: after 09-10 activity is 59% laundering, so "active late" would be a label proxy that inflates any detection metric. All 8 typologies survive |
| D26 | 09-27 | Demo seeds **20 attempts per typology** (`subgraph.attempts_per_typology`), chosen by content hash; the rest leave with their pairs | **User** chose "fewer laundering cases"; 20 set by measurement (M22) | Seeding every attempt put demo at twice the 2.50% ceiling (D6). 20 is the most per typology that clears it with margin |
| D27 | 09-27 | **Every AMLworld Cash pair is posted through the branch cash desk** (`internal_accounts.cash_desk.routes_payment_formats: [Cash]`), like the planted deposits. The desk is per region of the customer it serves | **User** (after the plain-language explanation of M23) | Genuine and planted cash must share one shape, or the desk is the label. It is also how core banking books cash |
| D28 | 09-27 | Drop and re-create the empty dev `RAW_AMLWORLD_TRANS` at `NUMBER(20,6)` during Phase 3 step 2, after re-checking it has 0 rows | **User** | M14; the only destructive step, approved once for that step |
| D29 | 09-27 | Planted deposits take their **clock time from the sample's genuine cash deposits** (`deposit_time_of_day: genuine_cash_deposits`, the empirical times, drawn per deposit); no clock window is configured | Build, within the approved step-1 residual-tell scan | M25 |
| D30 | 09-27 | **Cash is a deposit only:** a Cash pair becomes the single pair *payee's desk -> payee*, both legs DEPOSIT. The payer's side is not booked. Supersedes D27's first build (payer -> desk + desk -> payee) | **User** ("we genuinely don't know where that cash came from") | M24: booking the payer's withdrawal left a perfect twin. The payer-to-payee link survives only via `TXN_LABELS.src_row_number` -> `RAW_AMLWORLD_TRANS`; costs listed in step 1 below |
| D31 | 09-27 | Desk-routed deposits are always in INR (`retain_foreign_pct` does not apply to them) | Build, within the residual-tell scan | M26; an over-the-counter deposit at an Indian branch credits rupees |

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

### Step 5 -- entities + relationships
- `src/risk_copilot/generation/draws.py`: shared weighted draws from config distributions; `account_type()` used by both sampler and builder (D20).
- `src/risk_copilot/generation/entities.py`, `build_entities()`:
  - `RAW_PARTY_CBS`, `RAW_PARTY_KYC`, `RAW_ACCOUNTS` for every sampled party and account
  - every attribute drawn per AMLworld key; Faker re-seeded per entity
  - dates anchored to the first legit day (D19) and asserted to predate the window (`customer_since` respects `min_customer_age_years`; `opened_date` and `kyc_last_reviewed` fall between `customer_since` and the window start)
  - PAN encodes the holder type; Aadhaar and occupation for individuals only; `kyc_note` NULL
  - balance = lognormal opening + net window flow, floored at 0; credit limit only for credit products
  - **declared income** (GAP-04, D1): legit window inflow x 365 / legit span / floored-normal coverage x lognormal noise, under-declared for parties holding an illicit leg and for hosts, floored, rounded, no ceiling
  - exposes `legit_inflow_inr` for the `income_calibration` gate
- `src/risk_copilot/generation/relationships.py`, `build_relationships()`:
  - SHARED_PHONE pairs (the phone is copied)
  - BENEFICIAL_OWNER (legal person -> sampled individual)
  - SHARED_DEVICE clusters (GAP-05): co-located clusters grown along account flow edges with at least `fund_flow_min_txn_count` pairs; decoys whose parties never transact; both span >= 2 parties; unclustered accounts keep unique fingerprints
  - edge ids are hashed and asserted unique
- `conf/generation.yml`: locale (D21), `record_faker_version`, `dates`, `nationality`, statuses, branch template, `phone`, `pan_holder_code`, `source_of_funds`, `balance`, `credit_limit`, `income.coverage_ratio_min`, device cluster settings, `relationship_id_prefix`, `deposit_account_types`, `window_days: 7` (D18).
- Fixture: host `800A0000A` gained a third account (`10/8000A0102`, SAVINGS, no pairs) so it qualifies under D20. Its original LOAN account was left in place; a new account number was appended.
- Tests: `tests/unit/test_generation_entities.py` (21) + 1 graph test (deposit-account qualification). They cover:
  - one record per sampled key; no label column in any output
  - attribute stability across sample sizes
  - PAN / phone / Aadhaar shapes; Latin-script names (M20)
  - date ordering; legit-only window (M21)
  - income floor, rounding, monotonicity in inflow, and under-declaration of exactly the illicit set
  - phone copy; BO direction
  - co-located clusters connected by flow; decoys with no party-level flow; unique solo fingerprints
  - deterministic, unique edge ids; config guards
- **Real data** (sample windows measured before D19; the dev window is 09-01..09-10):

  | Profile | Time | Parties / accounts | Device clusters (co-located / decoy) | Shared-device accounts | Edges |
  |---|---|---|---|---|---|
  | dev | 0.2 s | 500 / 553 | 9 / 3 | 43 of 44 targeted | 60 device, 10 phone, 20 BO |
  | demo | 2.4 s | 5,000 / 10,000 | 153 / 60 | 800 of 800 | 1,138 device, 100 phone, 197 BO |

  Every record predates its window. Declared income: dev median Rs 7.7 cr, p90 Rs 291 cr; demo median Rs 3.7 cr. That is AMLworld's amount scale annualised, not a bug. It is recorded for the income rule's recalibration.

### Step 6 -- inject + eval grain + demo hero
- `src/risk_copilot/generation/inject.py`, `inject_structuring()`:
  - per host: 2-4 of its **deposit** accounts, 3-6 deposits each, in one `window_days` burst inside the legit window during branch hours (D22)
  - each deposit is Dr cash desk / Cr customer, with `amount_ctr_ratio` drawn in the band at 10 dp (D2); no rupee column exists
  - writes `INJECTED_TXN_PENDING` rows, TYP_STRUCTURING labels, and cash-desk `ID_MAP_PARTY` / `ID_MAP_ACCOUNT` rows for every configured region (D4)
  - fails if the window exceeds the legit span
  - returns a `HostPlan` per host as construction evidence
- `src/risk_copilot/generation/eval_grain.py`, `build_eval_grain()`: party-month rows over customer legs only (cash desk skipped), dominant typology plus the union of expected rules (D23), unattributed illicit expecting none.
- `src/risk_copilot/generation/demo_entity.py`, `select_demo_entity()`: constructed candidates only (D24), preferring party type then region, hash-ranked; writes `PROVISIONAL_UNTIL_DETECTED`.
- `conf/generation.yml`: `injection.structuring.deposit_hours`.
- Fixture: one legit host pair on 2022-09-10 (row 17), so the fixture's legit window is 10 days like the real ledger's; the graph hand trace was updated.
- Tests: `tests/unit/test_generation_inject.py` (16). They cover:
  - deposit-account targeting (the fixture host's LOAN account is never credited)
  - leg shape and signs; the cash desk on the debit side
  - band and scale of the ratio
  - burst and branch-hour timing
  - no rupee column
  - complete labels
  - desk isolation from customer tables
  - window guard; determinism
  - host and unattributed eval rows; leg totals; desk exclusion
  - hero selection, failure and region preference
- **Real data:**

  | Profile | Legit window | Deposits (hosts) | Total legs | Illicit rate | Hero |
  |---|---|---|---|---|---|
  | dev | 09-01..09-10 (10 d) | 98 (10) | 10,058 | **1.99%**, passes the 2.50% gate | P-754971478708, MUMBAI, 1 of 10 candidates |
  | demo | 09-01..09-18 (18 d) | 394 (40) | 133,008 | **5.44%**, fails | none: RAPID_CREDIT_VELOCITY / SHARED_DEVICE_CLUSTER not constructed (D24) |

  M18's dev projection (2.85%) overstated injection: most hosts own only 2 deposit accounts. Demo's legit window is 18 days because its sample does include 452 legit tail pairs (M21).

### Step 7 -- gates, writer, `riskcopilot generate`
- `src/risk_copilot/generation/validate.py`: 15 gates over the final tables. Blocking gates are those with `fail_build: true`; `income_calibration`, `benford` and `degree_distribution` only report. Clause values come from the `conf/detection.yml` seed that `policy` loads. No gate applies a detection rule; `signal_yield` checks construction and reports `DEFERRED_TO_LOAD` (D7).
- `label_leakage` also scans **values**: every RAW string must not contain a typology id, an injection label, the source label, or `src:` / `inj:` (substring match). The first version matched prefixes only, and the test caught `"note src:12"` slipping through.
- `src/risk_copilot/generation/writer.py`: Parquet (zstd, fixed row-group size) under `build/generated/<profile>/{raw,ground_truth}/`, plus `manifest.json`. The manifest holds a `result_hash` per table, the seed, source hashes, versions (Faker, pyarrow), the legit window, referenced clause ids, sampling / entity / injection stats, every gate, and `passed`. No wall-clock time is recorded.
- `src/risk_copilot/generation/pipeline.py`, `run_generation()`:
  - runs source -> sample -> reskin/labels -> entities -> relationships -> inject
  - allocates ids once over every pair (D16)
  - builds the 7 RAW and 4 GROUND_TRUTH tables with their DDL column names
  - runs the gates, writes the files (also when a gate fails, marked `passed: false`)
  - writes `conf/generated/demo_entity.<profile>.yml` only on a passing build
- `src/risk_copilot/cli.py`: `generate` is real (`--out-dir`, `--data-root`). It prints a gate table and exits 1 on any blocking failure or contract error.
- `conf/generation.yml`: an `output` block; `validation.label_leakage.forbidden_value_prefixes`; `src_row_number` added to the forbidden columns (it joins back to `RAW_AMLWORLD_TRANS.is_laundering`); `budget` and `demo_entity` gates; the base-rate comment corrected to 0.102% (M5).
- Tests: `tests/unit/test_generation_pipeline.py` (32). They cover:
  - an end-to-end fixture build, plus the real band blocking it
  - byte-identical files across runs; seed sensitivity
  - manifest contents
  - **every Parquet column set checked against its DDL table** (subset of the DDL columns, all NOT NULL columns without a DEFAULT present)
  - no label columns in RAW; injected legs kept out of RAW_TRANSACTIONS
  - **each of the 12 blocking gates violated in turn and shown to block**, with a meta-test that the violation list covers every blocking gate
  - label values caught inside strings; reporting gates never blocking
  - CLI exit codes 0/1 by gate state and on a contract error
- **`riskcopilot --profile dev generate`, real data:**

  | Gate | Status | Value |
  |---|---|---|
  | illicit_rate_pct | PASS | 1.988% (200 of 10,058 legs) |
  | bank_id_resolution | PASS | 0 unresolved legs |
  | label_attribution | PASS | `{1: 3209}`, 0 legit matches |
  | id_map_one_to_one | PASS | 501 parties / 556 accounts (incl. cash desk) |
  | injection_window | PASS | 7 d vs legit span 10 d, clause 30 d |
  | injection_band | PASS | [0.9, 0.985] within [0.9, 0.999] |
  | income_calibration | REPORT | ratio p50 0.0297 / p90 0.0487 / p99 0.0960 / max 0.1252 |
  | money_conservation | PASS | 0 unpaired of 5,029 pairs |
  | referential_integrity | PASS | 0 orphans |
  | benford | REPORT | chi2 107.9 (n = 4,929; limit 60, non-blocking) |
  | degree_distribution | PASS | alpha 1.96 |
  | label_leakage | PASS | 0 leaks (columns and values) |
  | budget | PASS | 500 / 553 / 10,058 vs 500 / 1,000 / 40,000 |
  | signal_yield | DEFERRED_TO_LOAD | 10 hosts constructed, each >= 2 accounts in band in window |
  | demo_entity | PASS | P-754971478708 "Kamya Dhar", MUMBAI, PROVISIONAL_UNTIL_DETECTED |

### Step 8 -- the real run, and the design doc brought in line with it
- `riskcopilot --profile dev generate` on the full HI-Small files: exit 0 at the first attempt, 18 s, every blocking gate passing (table in step 7). Two runs produced byte-identical files and pin.
- M4 and M5 were confirmed **through the generator itself**: `label_attribution` saw `{1: 3209}`, and the ledger reader counted 5,177 illicit rows. The deferred §S4/§S5 corrections could therefore be made.
- `docs/enhanced-architecture-diagrams.md` corrections:
  - BUILD STATUS §S1: date, Phase 2 **Done for the dev slice** (and why demo is blocked), Phase 3 **Next**, 365 tests, the `generation/` package and the fixture
  - §S1/§S4: table count **28 -> 38**, counted from the `CREATE TABLE` statements in `sql/01_ddl` (13 / 8 / 9 / 8)
  - §S4: the base rate 0.063% -> **0.102%** (M5), with the 3,209 / 1,968 split and the 0.10-2.50% band (D6); new rows for the 10-day legit window (M21), `en_IN` (M20), bank-id padding (M1), and the `source_ref` value leak (D16)
  - §S5 #3: account-level sampling (M17) and the measured sizes; #4: legit-span income and the measured ratio; #6: structuring as built (M6, D2/D4/D18/D20/D22); #7: the measured rates; #9: **1:1 attribution on the full key (M4)** replaces the assumed ambiguity, and the Patterns `:` finding (M3); #17: provisional, constructed hero (D24)
  - §25.1: the 0.063% line struck through with the measured figure; the date range annotated with M21; `hi_IN` -> `en_IN`
- **The two open decisions** were put to the user in plain terms and decided: cut at 09-10 (D25), and fewer laundering cases (D26, N = 20 by measurement, M22). Implemented as `source.window_end` and `subgraph.attempts_per_typology`; attempts are dropped with reason `after_window` / `not_selected` and their pairs leave with them. 6 new graph tests (the cut on the fixture, YAML date handling, the production cut leaving the fixture whole, per-typology selection including the unselected attempt's rows leaving, the config guard).
- **After the decisions, real data:**

  | Profile | Exit | Illicit rate | Parties / accounts / legs | Attempts | Blocking failures |
  |---|---|---|---|---|---|
  | dev | 0 | 1.99% (unchanged) | 500 / 553 / 10,058 | n/a (host-seeded) | none |
  | demo | 1 | **2.06%** | 5,000 / 8,857 / 163,992 | 160 (20 x 8), 109 cut, 101 not selected | `demo_entity` only (D24) |

  Demo's reported gates: benford chi2 565.0 and income ratio max 0.21, both non-blocking. Verification: 371 tests passed; ruff, mypy and lint clean.

## Departures from the plan

| Plan said | Built | Why |
|---|---|---|
| Add `internal_party_id` / `internal_account_id` to `ids.py` | Not added. The cash desk derives through the existing `party_id("INTERNAL:BANK")` and `account_id("INTERNAL", "CASH_DESK:<region>")` | Same determinism with less surface. The keys cannot collide with AMLworld's (numeric bank ids, hex entity ids; these contain letters and `:`), and `normalise_bank_id` leaves non-numeric ids intact. The combined 1:1 gate still covers collisions |
| `income_rule_yield` gate | `income_calibration`, which reports the generated inflow-to-declared distribution | Counting would-fire parties means applying the rule's threshold in Python, the same objection as D7. The distribution is a property of the data; firing is checked after load |
| "A ledger reader" (module unnamed) | `generation/source.py`, which also holds the accounts reader, the ownership map and manifest resolution | One module owns every positional source contract; `patterns.py` reuses its column tuple and format, so the two can't drift |
| Plan step 7's fixture list included a SoleProp host | Added in step 2 | Costs nothing now, and step 3 needs it |
| Plan left inclusion granularity open; step 3 first built party-closed inclusion | Account-level (D13) | M17: party-closed cannot fit any budget once launderers are seeded |
| Plan puts host ranking in `inject.py` | `graph.py` (D14); `inject.py` consumes `Subgraph.hosts` | Sampling must know the hosts to seed them |
| Gate list in the plan | Adds `budget` and `demo_entity` gates, and a value-level scan in `label_leakage` | Hard budgets and a missing hero must fail through the same path as every other contract; D16 showed a column check cannot see a value leak |
| "generate fails if a gate fails" | Files are still written, marked `"passed": false`; exit code 1; no demo pin | A failed build must be inspectable. **Phase 3 obligation: `load` refuses a manifest with `passed: false`** |

---

## Open items and risks

- **Illicit-rate calibration (M18) -- DECIDED (D26).** Demo seeds 20 attempts per typology. With the cut (D25) demo measures **2.06%**, inside the 2.50% ceiling; dev is unchanged at 1.99%.
- **Demo illicit rate:** the D6 estimate of ~2% assumed a ~400K-leg demo sample; the measured sample is 132K legs and 4.87% illicit before injection (M18). Superseded by the decision item above.
- **The illicit tail (M21) -- DECIDED (D25).** The ledger is cut at 2022-09-10, removing the 09-11..09-18 tail and the 109 attempts that reach into it.
- **Demo still exits 1, on `demo_entity` only.** Its `must_trigger_rules` include RAPID_CREDIT_VELOCITY and SHARED_DEVICE_CLUSTER, whose cohorts are not built (D24). Every data gate passes. This clears when those cohorts land, or if demo's rule list is narrowed -- a scope decision for that phase.
- **Income scale.** AMLworld amounts annualised give median declared incomes of Rs 3.7-7.7 crore, implausible for a retail book. They are consistent with the ledger, so `INCOME_ACTIVITY_MISMATCH` is unaffected in ratio terms, but a demo audience will notice. Revisit with the income rule's recalibration.
- **Income under-declare factor** is uncalibrated for 18 days (M12). Recalibrate when `INCOME_ACTIVITY_MISMATCH` lands.
- **ML velocity baseline** needs 45 days; the source has 18 (M2). This needs a decision before Phase 5.
- **Dormant reactivation** needs 180 days of dormancy (M2). It needs synthesised history or a redefinition before that cohort is built.
- **Phase 3 obligations created here:**
  - `load` must run after the `policy` seed stage (D2)
  - `DT_PARTY_RELATIONSHIP` must exclude `internal_accounts.src_bank_id` from FUND_FLOW (D4)
  - ground-truth files need a stage the service role cannot read
- **Phase 3 obligation (M14):** `RAW_AMLWORLD_TRANS.amount_received` / `amount_paid` are `NUMBER(20,2)`, so COPY would round Bitcoin's 6 dp values (`0.025852` -> `0.03`) in a table documented as verbatim. Snowflake cannot change a NUMBER column's scale in place, so the fix is `NUMBER(20,6)` in the DDL plus a drop and re-create of the (empty) dev table. That is a destructive step, so it is left for `load` and not applied now. Generated RAW tables are unaffected: Bitcoin is excluded before reskin (D9).
- **Commits:** everything without a credential is committed at the user's standing request, on `feature/ashwinAnalyze`, not pushed (`b8b94b4`, `85c862c`, then one commit per step). Before each commit, the actual secret values from `.env`, `connections.toml` and the key file are compared against every staged file; counts only are printed. `.env` stays gitignored.
- **`docs/enhanced-architecture-diagrams.md` §S4/§S5** corrected in step 8 from the measured facts (see step 8 below).

---

# Phase 3 -- load, dynamic tables, streams/tasks, replay harness

Plan: `.snowflake/cortex/plans/phase3-load-dt-replay.plan.md`, approved 09-27. Dev
thin slice only; demo `load` stays refused while its manifest says `passed: false`
(D24).

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Close the cash-desk tell in the generator (D27, D30), residual-tell scan (D29, D31), M18 test | **Done** |
| 2 | Contracts: DT lags, stages, config, `RAW_AMLWORLD_TRANS` at scale 6 (D28) | Next |
| 3 | `policy` seed stage | |
| 4 | `riskcopilot load` | |
| 5 | Dynamic tables for the structuring slice | |
| 6 | `SIGNALS_STREAM` + `ROUTE_SIGNALS` | |
| 7 | Post-load verify: signal_yield, hero promotion | |
| 8 | Replay harness, docs | |

## Findings made while planning (P1-P7)

| # | Finding | Source |
|---|---|---|
| P1 | The cash-desk label proxy | Measured -- recorded as M23 |
| P2 | `base.yml` lags are invalid: `DT_DIM_PARTY` (1 day) sits upstream of `DT_TRANSACTIONS_ENRICHED` (1 min / 60 min dev), `DT_PARTY_RELATIONSHIP` (1 day) upstream of `DT_SIGNALS_AGGREGATE` (60 min). Snowflake rejects a downstream table fresher than its upstream | Snowflake docs (target lag) |
| P3 | `DT_TXN_DAILY_AGG` is `DOWNSTREAM` with only a view as consumer, so it would never refresh; and a view cannot sit between two dynamic tables | Docs |
| P4 | `CURRENT_DATE()` in WHERE/QUALIFY is supported for incremental refresh, so an incremental table may join `V_POLICY_CLAUSE_CURRENT` -- to be measured | Docs |
| P5 | All 38 dev tables exist and hold 0 rows; `demo` has no database | Live, ZC14131 |
| P6 | Dev: 598 source cash pairs, 0 illicit, 1 legit in-band credit | Measured |
| P7 | A party's region is a pure hash draw on its AMLworld entity key | Code |

## Step 1 -- the cash desk stops being a label (D27, D29-D31)

**What changed**
- New `src/risk_copilot/generation/desk.py`: `CashDesk` owns the desk identity (moved out of `inject.py`), the per-region account, the routed payment formats, and the desk's `ID_MAP_*` rows. The pipeline now emits those rows itself, so they exist whether or not any cohort is injected.
- `draws.branch_region()`: the region draw, shared by entities and reskin (identical values to before).
- `reskin.py`: a pair whose format is in `routes_payment_formats` becomes *payee's regional desk -> payee*, both legs the format's credit type (DEPOSIT), always in INR (D30, D31). Every other pair is unchanged.
- `inject.py`: planted deposit times are drawn from the sample's genuine cash deposits (D29); `deposit_hours` is gone. `inject_structuring` now takes the reskinned legs. It fails rather than invent a clock if the sample has no genuine cash deposit.
- `entities.py` / `relationships.py`: legs on the desk are skipped (no customer owns them), which also keeps the desk out of the shared-device flow graph (D4).
- `validate.py`: `benford` and `degree_distribution` exclude the bank's own accounts; the desk would otherwise be one hub joined to every cash customer.
- `conf/generation.yml`: `internal_accounts.cash_desk.routes_payment_formats: [Cash]`, `injection.structuring.deposit_time_of_day`, and comments rewritten with the measurements.
- M18 cleanup: `test_dev_cohort_is_sized_for_the_illicit_rate_band` replaced by `test_worst_case_reserve_fits_the_leg_budget` (both profiles), which asserts what the division actually checked. The rate is left to the blocking `illicit_rate_pct` gate.

**What D30 costs** (the drawbacks of deposit-only)
- **The payer loses a debit.** A's account no longer shows the cash leaving: its window outflow and closing balance omit it. A is still a customer if it has other activity; on dev, the 598 routed pairs remove 598 payer-side legs.
- **The visible cash link between two customers is gone.** No customer-to-customer cash leg exists in RAW (measured: 0 on both profiles). A graph rule cannot follow cash hand-offs -- as a real bank cannot. The link is recoverable only for evaluation, via `TXN_LABELS.src_row_number` -> `RAW_AMLWORLD_TRANS` (admin-only).
- **Sampling still follows cash edges.** The subgraph BFS runs over the source ledger, so a payer can be sampled because of a cash hand-off that is not booked. That makes those parties slightly less connected in the visible data than their sampling reason suggests.
- **Illicit cash rows keep a label but lose a leg.** The ledger's 108 illicit Cash pairs (0 on dev) are labelled on the desk leg and the payee credit only. For graph typologies this is nothing: 3,208 of 3,209 attributed illicit rows are ACH (M6).

**Tests:** 378 passed (371 before). New or changed:
- cash pair shape (desk -> payee only, payer untouched);
- desk deposits forced to INR (a routed USD-to-USD pair);
- every desk leg on the cash channel, and only there;
- planted clock times drawn from genuine ones, plus the no-cash failure and the unknown-mode failure;
- the D27 config coherence: the routed format maps to the injected channel and txn_type;
- the reserve-fits-budget test.

Tests that had counted the desk as a customer were corrected: the eval-grain leg total, the shared-device flow helper, and the 100%-co-location test, which now allows the decoys the smaller flow graph forces.

**Real data**

| Profile | Legs (source + injected) | Illicit | Genuine desk deposits | Planted | Blocking failures |
|---|---|---|---|---|---|
| dev | 9,862 + 202 = 10,064 | **2.047%** (206) | 598 | 101 | none; hero P-754971478708 unchanged |
| demo | 163,182 + 812 = 163,994 | **2.057%** (3,374) | 7,822 | 406 | `demo_entity` only (D24) |

- Dev ran twice byte-identical (12 files).
- The tell checks all close on both profiles:
  - identical (txn_type, channel) on every desk leg;
  - 0 customer-to-customer cash legs;
  - 0 twins;
  - hours within 3 points of each other (M25);
  - 0 foreign desk deposits.
- The false-positive control for STRUCTURING_CROSS_ACCOUNT holds genuine in-band desk deposits: 1 on dev (1 account), 53 on demo (11 accounts).

**Departures from the Phase 3 plan**

| Plan said | Built | Why |
|---|---|---|
| Two pairs per cash row (payer -> desk, desk -> payee), 4-leg budget accounting in `graph.py` and `labels.py` | One pair, desk -> payee; `graph.py` and `labels.py` unchanged | D30 (M24). The two-pair version was built, measured, and removed |
| Desk helpers in `draws.py` | `branch_region` in `draws.py`; the desk in its own `desk.py` | The desk is identity, not a draw |
| Scan and fix the hour tell only | Hour (D29) and currency (D31) | The scan found both |

**Verification:** 378 passed; ruff, mypy (27 files), lint (dev + demo, 6 templates each) clean; `doctor --profile dev` exit 0, 0 failed. No SQL changed.
