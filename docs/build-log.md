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
| M27 | **The stages and file formats were owned by ACCOUNTADMIN.** `01_account_objects.sql` creates them under that role and `02_roles_grants.sql` handed over only the database and schemas: all 4 stages and all 4 file formats on dev had owner ACCOUNTADMIN. The admin role could list a stage only through a human session's secondary roles, which a task does not have | `SHOW STAGES` / `SHOW FILE FORMATS IN DATABASE` as the admin role | Ownership handed to the admin role in bootstrap; verified live after the change |
| M28 | **Not every pair balances in INR, and correctly so.** 122 of 4,931 dev pairs have a non-zero `amount_inr` sum; all 122 are AMLworld rows whose payment and receiving currencies differ (0 same-currency), so each side's real amount converts at our rate to a different rupee value. The plan's "every pair sums to 0" was wrong as stated | Local join of the unbalanced pairs to the source ledger; re-asserted in Snowflake by `load` (0 unexplained) | Post-load check is "every unbalanced pair is a cross-currency source row" |
| M29 | **Snowflake reads a Parquet `timestamp[ms]` as epoch milliseconds.** Without logical types, `$1:col` is an INTEGER (`1662650340000`); `::TIMESTAMP_NTZ` reads it as seconds and gives year **54657**. `TO_TIMESTAMP_NTZ(n, 3)` gives the right `2022-09-08 15:19`. `decimal128` reads exactly (sum of `amount_inr` equal to the paisa), `date32` reads as DATE, `list<string>` as ARRAY, JSON text as VARCHAR | Probe on the real dev files on a scratch stage path, removed after | `load` builds each timestamp cast from the file's own time unit |
| M30 | **STRUCTURING_CROSS_ACCOUNT fires on exactly the planted hosts.** `DT_SIGNALS_FAST_DEV` holds **10** signals, all in bucket `2022-08-26`: **10 of 10** on injected hosts, none elsewhere. Their **101** evidence legs are all injected deposits (101 illicit, 0 genuine). The one genuine in-band deposit (P6) does not fire, being a single account. metric_value is 2 distinct accounts for 9 hosts and 3 for one, against the clause's 2 | SQL on the refreshed tables, joined as admin to INJECTED_TXN_PENDING and TXN_LABELS | Precision and recall are formalised in step 7 |
| M31 | **Routing is idempotent under a full re-feed.** On a new, empty `SIGNALS_STREAM_DEV` (DELTA, not stale), `CALL ROUTE_SIGNALS_DEV(TRUE)` wrote **10 signals, 10 cases, 10 links** (0 updated, 0 escalated). A second identical call wrote **0 / 0 / 0 and updated 0**, so the unchanged-evidence guard holds on the ARRAY/VARIANT columns too. `EXECUTE TASK` twice: both **SKIPPED** (`0040003`, WHEN evaluated false, stream empty), +0 rows. Row checks: 0 SIGNAL-vs-DT differences (evidence ids, metric, threshold, detail, fired_at = last_txn_at); every `signal_id` / `case_id` equals `ids.signal_id` / `ids.case_id`; all rows `rule_version` 1 / `HIGH` = RULE_DEFINITION; one case per party, all `AML` / `OPEN`, `branch_region` = DT_DIM_PARTY (DELHI, MUMBAI, PUNE); sla_deadline, suspicion and priority NULL on all 10; the hero has `CASE-4399c4c86db5c6ea`. The task body block, run by hand, returns the router's counts (stream mode, all 0) | Live on RISK_COPILOT_DEV as the admin role, after two deploys of the stage | The stream's delta path and `SYSTEM$SET_RETURN_VALUE` run only in a triggered run: proven in step 8, when replay changes the table |
| M32 | **The signal yield is in range, and verify's refresh cascades as documented.** `STRUCTURING_CROSS_ACCOUNT`: **10** signals against dev's `signal_yield` [**1**, **2000**] -> OK (demo's floor is 5). One `ALTER DYNAMIC TABLE DT_SIGNALS_FAST_DEV REFRESH` returned `refreshed_dt_count 0, "No new data"`, and `DYNAMIC_TABLE_REFRESH_HISTORY` shows 3 `MANUAL` / `SUCCEEDED` / `NO_DATA` refreshes at **one** data timestamp: SIGNALS_FAST (645 ms), and its upstream TRANSACTIONS_ENRICHED (1,247 ms) and DIM_PARTY (990 ms). TXN_DAILY_AGG and PARTY_RELATIONSHIP, which are not upstream of it, were not refreshed | `riskcopilot --profile dev verify`, then refresh history as the admin role | `verify` refreshes only the signal tables some registered rule writes to |
| M33 | **Recall 10/10 and precision 10/10 at party-month; the false-positive control does not fire.** The grain holds **494** party-months, all 2022-09: **14** illicit, **10** expecting the rule, and 4 illicit with no expected rule (AMLworld legs, which count against no rule). The 10 signals map to **10** party-months (D40), all in the grain and all expected: TP 10, FP 0 (legit 0, other typology 0, not in grain 0), missed 0, 0 unresolved evidence ids. Control: **1** party-month with a genuine in-band cash credit (P-600485713464, 2022-09, 1 account), **0** fired. Mapping by the window's start month instead would have scored **0/10** both ways: every signal's bucket is 2022-08-26, every one of its 101 evidence legs is in 2022-09 | `sql/verify/03_detection_metrics.sql` as the admin role; the pre-build probe reproduced the same counts by hand | D40 |
| M34 | **The hero is confirmed, and the result is deterministic.** P-754971478708: `CONFIRMED_BY_DETECTION` with `STRUCTURING_CROSS_ACCOUNT\|P-754971478708\|2022-08-26` on `CASE-4399c4c86db5c6ea` (M31's case). Two runs wrote a byte-identical `demo_entity.dev.verified.yml` (SHA-256 `C7A1BDD3...`); generate's pin is unchanged (`30EFF57A...`, no git diff). The loaded injected legs carry the pin's seed, 20260922, and no other | Two live runs; file hashes | D41 |
| M35 | **Verify leaves the pipeline as it found it.** After two runs: `SIGNALS_STREAM_DEV` empty (`SYSTEM$STREAM_HAS_DATA` false, not stale); SIGNAL / CASE_RECORD / CASE_SIGNAL still 10 / 10 / 10, with `MAX(last_seen_at)` unchanged at 2026-09-27 05:02:28; ROUTE_SIGNALS_TASK_DEV still suspended. `GROUND_TRUTH.EVAL_RUN` gained exactly 2 rows (`EVAL-94053aede26b40e8`, `EVAL-c93d61c8badc45b7`): suite DETECTION, passed TRUE, seed 20260922, commit `294265a...-dirty` (the uncommitted step-7 tree). Runs took 12.8 s and 3.2 s | Live, as the admin role | D39, D42 |

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
| D32 | 09-27 | Dynamic-table lags: `dim_party`, `party_relationship` -> `DOWNSTREAM`; `txn_daily_agg` gets a real lag equal to `signals_fast` (dev 60 min, demo 5 min). Each table declares `upstream`, and the config refuses to load a graph with a table fresher than its upstream, a `DOWNSTREAM` table nobody reads, a cycle or an unknown name | Build (plan P2, P3) | Snowflake rejects the first at CREATE and never refreshes the second, silently. A check at load keeps the next edit from reintroducing either |
| D33 | 09-27 | The policy seed is **insert-only for versioned governed rows** (POLICY_CLAUSE, RULE_DEFINITION): a live row that differs from `conf/detection.yml` fails the deploy before anything is written. TYPOLOGY and REF_CHANNEL are unversioned reference data and upserted. RULE_DEFINITION lists only rules whose SQL is built (STRUCTURING_CROSS_ACCOUNT) | Build | Editing the seed and redeploying must not be a way round the clause-versioning gate. A registry row for an unbuilt rule would let a finding cite logic nobody wrote |
| D34 | 09-27 | `party_relationship` gets a real lag (60 min) instead of `DOWNSTREAM` | Build | Its only designed consumer, DT_SIGNALS_AGGREGATE, is not built; a `DOWNSTREAM` table with no deployed consumer never refreshes. D32 checks the design graph, so a test now checks the deployed one |
| D35 | 09-27 | **Each rule's detection SQL is one file, `sql/rules/<rule_id>.sql`**, included by its signal table; `RULE_DEFINITION.rule_sql_hash` is the SHA-256 of that source (line endings normalised), bound once while NULL. A changed file then fails the policy stage until `rule_version` is bumped; a registered rule without a file fails the render. Rules name their clauses by role (`ctr`, `band`, ...) so the SQL reads clause ids from config | Build | Makes "a silent logic change is visible" (the column's purpose) and "only built rules are registered" (D33) mechanical rather than a matter of discipline |
| D36 | 09-27 | **`ROUTE_SIGNALS(full_scan BOOLEAN DEFAULT FALSE)`**: the stream's INSERT rows normally, the whole `DT_SIGNALS_FAST` when TRUE. The full scan is run once after the first deploy, by the replay harness, and to recover a stale stream. It reads the stream as well, so it also consumes it | **User** (after the "doorbell fitted to a full house" explanation) | A stream on a dynamic table cannot take `SHOW_INITIAL_ROWS` or `AT/BEFORE` (CREATE STREAM reference), so rows already in the table never reach a new stream. Per-run work stays proportional to change |
| D37 | 09-27 | **A re-fire refreshes the evidence**: metric, threshold, evidence ids, detail, fired_at, rule_version, severity, as_of_ts and last_seen_at; `signal_id` and `first_seen_at` are kept. A row whose evidence is unchanged is not written, so `last_seen_at` is the last time the evidence *changed* | **User** | The row always shows the latest evaluation, and step 8's replay-vs-batch equivalence can hold. Findings cite their own query snapshots (GAP-12), so they do not depend on SIGNAL staying frozen |
| D38 | 09-27 | **`ROUTE_SIGNALS_TASK` is triggered** (`WHEN SYSTEM$STREAM_HAS_DATA`, no SCHEDULE); `route_signals_schedule` is deleted. `resume_route_task` (base and dev false, demo true) decides whether a deploy leaves it running | **User** | It runs within about 30 s of a refresh that changed a signal and never otherwise, and Snowflake's 12-hourly health check keeps the stream from going stale. A leftover schedule key now fails the config load |
| D39 | 09-29 | **`verify` is read-only for the pipeline**: it refreshes the signal tables and reads them, and never calls `ROUTE_SIGNALS`. The hero's case id is read from CASE_SIGNAL; a hero signal on no case is still confirmed, with a warning | **User** (after the "inspector presses the test button, does not phone the fire brigade" explanation) | `ROUTE_SIGNALS` consumes `SIGNALS_STREAM` (D36): a verify run between a replay and the task would swallow the delta step 8 must prove the task routes |
| D40 | 09-29 | **A signal counts in the calendar month of each of its evidence transactions**, month read from `txn_timestamp` in the grain's own format (`eval_grain.MONTH_BUCKET_SQL_FORMAT`, shared with the Python builder) | **User** | Signals are keyed by a 30-day window bucket and the grain by month. The evidence dates are what generation labelled; the window's start month would score 0 (M33), and "every month the window overlaps" credits months with no evidence |
| D41 | 09-29 | **The hero result goes to a sibling file**, `conf/generated/demo_entity.<profile>.verified.yml` (`output.demo_entity_verified_file`), written only by `verify` and committed: status, party, seed, per-rule signal keys and case ids, missing rules, no clock time. Generate's pin is never rewritten | **User** | One writer per file: generate's pin stays byte-identical per seed, and the result is deterministic for the same data (M34). The result names the pin's party and seed, so a stale one is recognisable |
| D42 | 09-29 | **One `GROUND_TRUTH.EVAL_RUN` row per verify run** (suite DETECTION): yield, metrics, hero, refresh result, git commit (`-dirty` when tracked files differ), seed, passed | **User** | "A number without a commit is not a result" (the table's own comment). GROUND_TRUTH is admin-only, which suits metrics computed from labels |
| D43 | 09-29 | `verify` **refuses with exit 2**, before judging anything, a pin that disagrees with config (status, profile, seed, must-trigger rules) or loaded data built from another seed. A rule's false-positive control is an optional `sql/verify/controls/<rule_id>.sql`; without one the report says "none defined", not "0" | Build | Confirming a pin against another build's data would certify the wrong party. A mandatory control file would block registering a rule whose near-miss is not yet defined |

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
- **Phase 3 obligation (M14) -- DONE in Phase 3 step 2 (D28).** `RAW_AMLWORLD_TRANS.amount_received` / `amount_paid` were `NUMBER(20,2)`, so COPY would have rounded Bitcoin's 6 dp values (`0.025852` -> `0.03`) in a table documented as verbatim. Now `NUMBER(20,6)`; the empty dev table was dropped and re-created. Generated RAW tables are unaffected: Bitcoin is excluded before reskin (D9).
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
| 2 | Contracts: DT lags, stages, config, `RAW_AMLWORLD_TRANS` at scale 6 (D28) | **Done** |
| 3 | `policy` seed stage | **Done** |
| 4 | `riskcopilot load` | **Done** |
| 5 | Dynamic tables for the structuring slice | **Done** |
| 6 | `SIGNALS_STREAM` + `ROUTE_SIGNALS` | **Done** |
| 7 | Post-load verify: signal_yield, hero promotion | **Done** |
| 8 | Replay harness, docs | Next |

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

## Step 2 -- contracts: lags, stages, grants, scale-6 ledger

**What changed**
- `conf/base.yml`: the lag fixes and `upstream` graph (D32); `stages.ground_truth: GROUND_TRUTH_STAGE`. `conf/profiles/dev.yml`: `txn_daily_agg` 60 minutes, matching dev's `signals_fast`.
- `config.py`: `DynamicTableSpec.upstream` and `.lag_seconds`; `_check_lag_graph` runs in `Settings._check_consistency`; `Stages.ground_truth`.
- `sql/00_bootstrap/01_account_objects.sql`: the ground-truth stage in `{{ fq.ground_truth }}` (SSE, no directory).
- `sql/00_bootstrap/02_roles_grants.sql`:
  - ownership of all 5 stages and 4 file formats handed to the admin role (M27);
  - `GRANT EXECUTE TASK ON ACCOUNT` to the admin role, which the routing task needs in step 6.
- `sql/01_ddl/01_raw_landing.sql`: `amount_received` / `amount_paid` `NUMBER(20,6)` (M14); the `src_row_number` comment now states the M16 contract.
- `sql/README.md`: `{{ stage.ground_truth }}`.
- **Moved to later steps** (from the plan's step-2 list, so no config lands before something reads it): the seed rows and routing settings in `sql_context` -> step 3; the `replay` block -> step 8; `snowflake_timestamp_format` -> step 4; `streams_tasks.resume_route_task` -> step 6.

**Live, on RISK_COPILOT_DEV**
1. `RAW_AMLWORLD_TRANS` re-checked: 0 rows. Dropped (D28), and nothing else.
2. `deploy --stage bootstrap --stage ddl` run twice: every file ok both times (bootstrap 25 + 115 statements, ddl 14 / 10 / 10 / 10).
3. Verified afterwards:
   - amounts `NUMBER(20,6)`;
   - all 5 stages and 4 file formats owned by `RC_ADMIN_DEV`;
   - `GROUND_TRUTH_STAGE_DEV` carries only its OWNERSHIP grant;
   - `EXECUTE TASK` held by `RC_ADMIN_DEV`;
   - the service role has no grant on anything in GROUND_TRUTH;
   - 38 base tables.

**Tests:** 387 passed (378 before). New:
- the configured lag graph is valid for both profiles;
- the original bugs are rejected: a table fresher than its upstream (the 1-day `DT_DIM_PARTY`), and a `DOWNSTREAM` table nobody reads (the old `DT_TXN_DAILY_AGG`);
- a `DOWNSTREAM` table is judged at its consumers' rate;
- unknown upstream and cycles are rejected;
- the ground-truth stage is created once and owned by the admin role, and no GRANT in any stage names GROUND_TRUTH except ownership to the admin role;
- every stage and file format is handed to the admin role;
- the idempotency test now covers `dynamic_tables`, `streams_tasks` and `policy`, and also rejects DROP / TRUNCATE / DELETE statements.

**Departures:** the plan's step-2 config additions were moved to the steps that consume them (above). M27 was not in the plan; found by inspecting the live stage owners before `load` depends on them.

**Verification:** 387 passed; ruff, mypy (27 files), lint clean; `doctor --profile dev` exit 0, 0 failed.

## Step 3 -- the `policy` seed stage (D33)

**What changed**
- `conf/detection.yml`:
  - new `rules` block: STRUCTURING_CROSS_ACCOUNT v1, TYP_STRUCTURING, its 4 clauses, PARTY_WINDOW, `window_clause_id: STRUCTURING_WINDOW_DAYS`, `window_anchor: "1970-01-01"`;
  - new `channels` block: the 6 rails, CASH the only `is_cash`, `typical_max_inr` null rather than quoted from memory.
- New `src/risk_copilot/policy.py`:
  - `sql_literal(value, type)` is the one escaping function. It doubles quotes and backslashes, casts every literal (so an all-NULL column keeps its type in the UNION ALL), and writes numbers through `Decimal(str())` so `0.999` stays `0.999`.
  - `policy_seed(detection)` validates, then returns `{table: [{column: literal}]}`. It refuses a clause without `provenance.source_document`, a rule with no/unknown clauses, an unknown typology, a missing severity, a bad grain or window clause, duplicate keys, and an empty block.
  - `RULE_DEFINITION.window_bucket_expr` names the anchor and the window clause, never the length.
- `config.sql_context()` exposes `policy_seed`; `sql/README.md` documents it as usable by `05_policy` only.
- New `sql/05_policy/01_seed.sql`:
  - each table's rows go into a session TEMPORARY table;
  - for POLICY_CLAUSE and RULE_DEFINITION, a Snowflake Scripting drift check raises -20001 / -20002 before the insert-only MERGE;
  - TYPOLOGY and REF_CHANNEL are MERGE-upserted;
  - RULE_DEFINITION is written after TYPOLOGY, because the foreign key is not enforced.
  - `rule_sql_hash` is left NULL here; it is bound when the rule's SQL is deployed (step 5).
- **Test fix found on the way:** the idempotency test compared the *raw* statement text, and every statement in these files opens with its header comment, so `startswith("CREATE")` never matched and the check had been vacuous. It now strips leading comment lines. Re-run over bootstrap and DDL, it still passes: every CREATE there was idempotent.

**Live, on RISK_COPILOT_DEV**
- `deploy --stage policy` run twice: 11 statements ok both times. The second run's drift checks passed, so live equals seed.
- Counts: 15 clauses, 11 typologies, 1 rule, 6 channels. `V_POLICY_CLAUSE_CURRENT` returns 15 rows, one of them CTR.
- The CTR row equals the seed on threshold (1,000,000), source document and effective date. STRUCTURING_BAND is [0.9000, 0.9990].
- CASH is the only `is_cash` channel.
- The RULE_DEFINITION row carries its 4 clause ids as an ARRAY, severity HIGH, and the bucket expression.
- **The drift check was proved to fire.** A seed rendered with the CTR threshold changed to 900,000 was loaded into the temporary table and only the drift block was run: it raised -20001. The live CTR stayed 1,000,000; no MERGE ran.

**Tests:** 417 passed (387 before). New `tests/unit/test_policy_seed.py` (30):
- literal escaping (quotes, backslashes, float exactness, dates, arrays, typed NULL) and type refusal;
- one row per config entry (15 clauses);
- provenance and exact threshold carried;
- only built rules registered;
- every rule governed;
- 10 refusal cases;
- every channel `generation.yml` can emit is catalogued, and the injected channel is `is_cash`;
- one SELECT per row, on its own line (a lost newline had fused rows at first render);
- **no other stage's rendered SQL carries a threshold value.**

**Departure:** `window_clause_id` was added to the rule, so the bucket expression names its clause explicitly instead of the code guessing which of the four it is.

**Verification:** 417 passed; ruff, mypy (28 files), lint (dev + demo, 7 templates each) clean; `doctor --profile dev` exit 0, 0 failed.

## Step 4 -- `riskcopilot load`

**What changed**
- New `src/risk_copilot/pipeline/load.py`:
  - `open_bundle` runs the offline refusals **before any connection**: `passed: false`, a profile mismatch, and any file whose rows or content hash differ from the manifest.
  - `Loader.preflight` runs the live refusals:
    - `RAW_AMLWORLD_TRANS` amounts not at `source.ledger.amount_scale`;
    - an injected CTR clause not in force;
    - an injected leg not in the base currency;
    - a ground-truth stage granted beyond ownership;
    - any non-empty target table, unless `--replace` is given.
  - `upload` routes files by zone:
    - RAW to `GENERATED_STAGE/<profile>/<manifest hash>/raw`;
    - ground truth to `GROUND_TRUTH_STAGE/<profile>/<manifest hash>/ground_truth` only;
    - the AMLworld CSVs, gzipped, to `DATA_STAGE/amlworld/<sha256>/`.

    All paths are content-addressed, so re-uploading the same content is skipped by name.
  - `run` does everything else in **one transaction**, rolled back on any failure:
    - `--replace` deletes;
    - COPY every table (`FORCE = TRUE`, since COPY's load history must not silently skip a re-load);
    - promote the pending legs;
    - verify.
  - Parquet COPY takes its column list and casts from the live `INFORMATION_SCHEMA`, and refuses a file column the table lacks (the shape of a leak). Timestamps are cast from the file's time unit (M29); JSON text going into a VARIANT is parsed.
  - The ledger COPY is positional (`$1..$11`) with `METADATA$FILE_ROW_NUMBER` under `FF_CSV_HEADER` (obligation 5, M16), using the new `source.ledger.snowflake_timestamp_format`.
  - Promotion is `INSERT ... ROUND(amount_sign * amount_ctr_ratio * c.threshold_lower, ?)` joined to `V_POLICY_CLAUSE_CURRENT`, with the decimals bound. It fails unless every pending leg is promoted, then sets `resolved_at` (obligation 3).
- `cli.py`: the `load` command (`--out-dir`, `--data-root`, `--replace`, `--source/--no-source`). It prints table row counts and the post-load checks.
- `data/MANIFEST.yml`: `laundering_rows: 5177` (M5), so the post-load check compares against a declared value, not a literal.
- `pyproject.toml`: S608 ignored for `load.py`, with the reason (identifiers only, all data bound), following the repo's per-file convention.

**Live, on RISK_COPILOT_DEV** (exit 0, 120 s including the 475 MB ledger upload)

| Check | Result |
|---|---|
| Row counts, all 13 tables | equal the manifest: RAW_TRANSACTIONS 10,064 (9,862 + 202 promoted), TXN_LABELS 10,064, ID maps 501 / 556, parties 500, accounts 553, KYC 500, edges 90, eval grain 494, pending 202, patterns 370 |
| Ledger (M5) | 5,078,345 rows, 5,177 laundering, both equal to `data/MANIFEST.yml` |
| Accounts | 518,581 |
| Every leg labelled | 0 unlabelled |
| **M16 in practice** | all 9,862 labelled source legs join `RAW_AMLWORLD_TRANS` on `src_row_number`: 0 missing, 0 timestamp, 0 `is_laundering`, 0 account mismatches (bank ids normalised as in `ids.normalise_bank_id`, desk legs excluded) |
| Pair balance (M28) | 122 unbalanced, 0 not explained by a cross-currency source row |

Independent checks afterwards:
- all 202 promoted legs equal `ROUND(sign x ratio x 1,000,000, 2)` read live (0 mismatches), `amount = amount_inr`, amounts Rs 9,00,080.95 to Rs 9,84,700.16;
- RAW timestamps 2022-09-01 to 09-10;
- 145,670 ledger `amount_paid` values keep more than 2 dp (M14 holds);
- 0 `src:` / `inj:` tokens in RAW_TRANSACTIONS, and no ratio or cohort column;
- no ground-truth file on the generated stage (7 files there, 4 on the ground-truth stage, 2 on the data stage);
- a second `load` is refused ("Target tables are not empty"), with nothing committed;
- **obligation 1, live:** `--profile demo load` is refused in 2 s with `passed: False (failed gates: ['demo_entity'])`, before any connection (demo has no database).

**Tests:** 436 passed (417 before). New `tests/unit/test_load.py` (19), against a real bundle written by the generator's writer and a recording fake session:
- every refusal: failed build, other profile, altered file, scale 2, policy not seeded, stage granted, non-empty without `--replace`;
- the CLI refuses a failed build without opening a session;
- millisecond timestamps, JSON text into a VARIANT, an extra column refused;
- the positional ledger COPY with `METADATA$FILE_ROW_NUMBER`;
- promotion joins the clause and writes no threshold;
- ground-truth files go only to the ground-truth stage;
- rollback on a failed check or an unpromoted leg;
- the decimals are bound.

**Departures from the plan**

| Plan said | Built | Why |
|---|---|---|
| "Every pair sums to 0" | Every unbalanced pair must be a cross-currency source row | M28 |
| `TRUNCATE` under `--replace` | `DELETE` inside the load transaction | A failed replace rolls back to the old data instead of leaving empty tables |
| Hold back source legs at or after `replay.hold_back_from` | Not in this step | The `replay` config lands with the harness (step 8); load gains the hold-back there |
| Refusals, then connect | Offline refusals before the session opens | So a failed demo build is refused although demo has no database |

**Verification:** 436 passed; ruff, mypy (29 files), lint clean; `doctor --profile dev` exit 0, 0 failed.

## Step 5 -- dynamic tables for the structuring slice (D34, D35)

**What changed**
- New `sql/02_dynamic_tables/`, all `CREATE OR ALTER`, on `{{ wh.pipeline }}`, `REFRESH_MODE = INCREMENTAL` (explicit, so a construct that forces FULL fails the deploy instead of quietly costing credits):
  - `01_dim_party.sql`: CBS + KYC, current state (`DOWNSTREAM`);
  - `02_transactions_enriched.sql`: every leg with party, branch, account type, `is_cash` / `is_instant` from REF_CHANNEL, and `is_internal_account` / `is_internal_counterparty` from `{{ internal.bank_id }}`. All joins LEFT, so no leg can vanish from CURATED. **No threshold is joined here**: the plan put the in-band flag in this table, but a missing clause would then remove rows every consumer reads, so the band test lives in the rule;
  - `03_txn_daily_agg.sql`: customer account x day, with `conf/ml.yml`'s contract columns (`account_id`, `txn_date`, `credit_amount_inr`); desk excluded;
  - `04_party_relationship.sql`: OWNS_ACCOUNT, FUND_FLOW (debit legs, **desk excluded on both sides**, self-flows dropped; obligation 6) and the attribute edges;
  - `05_signals_fast.sql`: UNION ALL of every rule assigned to `signals_fast`, each from `sql/rules/`;
  - `06_views.sql`: `V_TRANSACTION_PAIRS`;
  - `_macros.sql` (not deployed on its own): quoting of `TARGET_LAG`.
- New `sql/rules/STRUCTURING_CROSS_ACCOUNT.sql` (D35):
  - tumbling buckets from the anchor;
  - band, CTR, minimum accounts and window all read from `V_POLICY_CLAUSE_CURRENT` through the role-keyed clause ids;
  - emits `signal_business_key` in `ids.signal_business_key`'s format, the metric, the threshold, sorted evidence txn ids, and a `detail` object.
- `conf/detection.yml`: rule `clauses` keyed by role, and `dynamic_table: signals_fast`. The rendered `clause_ids` array is unchanged, so the live row still matches (pinned by a test).
- `conf/base.yml`: `party_relationship` 60 minutes, upstream `[transactions_enriched]` (D34).
- `policy.py`: `rule_context`, `rule_sql_path` / `rule_sql_hash`, and `policy_seed(detection, sql_root)` carrying the hash. The seed template's drift check accepts a NULL live hash, and a one-time UPDATE binds it.
- `config.sql_context()`: `rule` and `internal`. `deploy.py`: a `FileSystemLoader` for includes, and `lint()` also lints `sql/rules/` (forbidden names, no `;`), which `render()` cannot see inside an include.
- **Not built, by decision:** `V_TXN_VELOCITY_FEATURES`. Its rolling windows need lengths no config declares, and inventing them would be a hardcoded value. It lands with the velocity rule. VELOCITY_1H, SANCTIONS_CONSOLIDATED and SIGNALS_AGGREGATE stay deferred, as planned.

**Live, on RISK_COPILOT_DEV**
- `deploy --stage dynamic_tables` run twice: 6 files ok both times (14 s, then 6 s).
- `SHOW DYNAMIC TABLES` (P4 measured):

  | Table | Lag | refresh_mode | reason | State | Rows |
  |---|---|---|---|---|---|
  | DT_DIM_PARTY_DEV | DOWNSTREAM | INCREMENTAL | none | ACTIVE | 500 |
  | DT_TRANSACTIONS_ENRICHED_DEV | 1 hour | INCREMENTAL | none | ACTIVE | 10,064 |
  | DT_TXN_DAILY_AGG_DEV | 1 hour | INCREMENTAL | none | ACTIVE | 2,745 |
  | DT_PARTY_RELATIONSHIP_DEV | 1 hour | INCREMENTAL | none | ACTIVE | 1,198 |
  | DT_SIGNALS_FAST_DEV | 1 hour | INCREMENTAL | none | ACTIVE | 10 |

  The `V_POLICY_CLAUSE_CURRENT` join (a `CURRENT_DATE` QUALIFY view) did not force FULL.
- Detection: M30.
- ENRICHED: 10,064 rows = RAW; 699 desk legs; 0 unknown channels; 0 customer legs without a region.
- Edges: OWNS_ACCOUNT 553, FUND_FLOW 555, SHARED_DEVICE 60, BENEFICIAL_OWNER 20, SHARED_PHONE 10. **0 FUND_FLOW edges touch the desk party.**
- CURATED: 76 columns, **none** of the 10 `label_leakage.forbidden_columns`.
- `deploy --stage policy` run twice: 12 statements ok. `RULE_DEFINITION.rule_sql_hash` is bound, and equals the local file's SHA-256.
- **The hash lock was proved to fire.** The seed was rendered from a copy of `sql/` with one comment line appended to the rule file, and only the rule drift block was run: it raised -20002, and the live hash was unchanged.

**Tests:** 460 passed (436 before). New `tests/unit/test_dynamic_tables.py` (11 tests, 9 of them run for dev and demo: 20 cases):
- exactly the built tables are deployed;
- **every deployed `DOWNSTREAM` table is read by another deployed table**;
- lags as configured;
- all INCREMENTAL on the pipeline warehouse;
- nothing in CURATED reads GROUND_TRUTH;
- FUND_FLOW excludes the desk on both sides;
- the desk flag comes from the configured bank id;
- every rule sits in its dynamic table;
- the rule SQL has no number beyond a cast and the window's +/- 1, and reads every role's clause;
- the business key matches `ids.signal_business_key`;
- lint catches a bad included rule file.

In `test_policy_seed.py`: the rule row is unchanged by the role keys; the hash equals the file's, and is identical under CRLF; a rule with no SQL is refused. `test_retention_clamp.py`'s fake repository now carries `sql/rules/`, which the mechanical "only built rules" check made necessary.

**Departures**

| Plan said | Built | Why |
|---|---|---|
| In-band flag in DT_TRANSACTIONS_ENRICHED | In the rule SQL | A missing clause must not remove rows from CURATED |
| `V_TXN_VELOCITY_FEATURES` with channel entropy | Not built | No configured window lengths (no invented values) |
| `party_relationship` `DOWNSTREAM` (D32) | 60 minutes | D34 |
| rule_sql_hash left NULL | Bound from `sql/rules/` | D35 |

**Credits:** the five tables are ACTIVE on dev lags (DIM_PARTY on demand, the rest hourly). With no new RAW data, a scheduled refresh has nothing to process.

**Verification:** 460 passed; ruff, mypy (29 files), lint (dev + demo, 13 templates each) clean; `doctor --profile dev` exit 0, 0 failed.

## Step 6 -- signal routing (D36-D38)

**Found before building:** the CREATE STREAM reference gives the dynamic-table form with neither `SHOW_INITIAL_ROWS` nor `AT/BEFORE`. A new stream therefore starts empty, and the 10 signals already in `DT_SIGNALS_FAST_DEV` would never reach it. That is the reason for D36.

**What changed**
- New `sql/03_streams_tasks/01_route_signals.sql` (4 statements in dev, 5 in demo):
  - `SIGNALS_STREAM` in CURATED: `CREATE STREAM IF NOT EXISTS ... ON DYNAMIC TABLE`. Never `OR REPLACE`, which would reset the offset and drop unrouted changes on every deploy.
  - `ROUTE_SIGNALS(full_scan)` in EVIDENCE_CHAIN: Snowflake Scripting, owner's rights, **one transaction** (any error rolls back and re-raises, leaving the stream unconsumed). It:
    1. refuses a batch row whose rule has no active RULE_DEFINITION row (-20003);
    2. MERGEs into SIGNAL on `signal_business_key`, taking `rule_version` and `severity` from the highest active registry version and refreshing the evidence only when it changed (D37). `fired_at` = the latest evidence transaction, `as_of_ts` = the routing run;
    3. opens one case per party that is owed one and holds no case in `open_case_statuses`. "Owed" means a signal at or above `min_severity_for_case` that is on no case yet. `branch_region` comes from DT_DIM_PARTY (NOT NULL, so a missing party fails the run); `sla_deadline`, suspicion and priority stay NULL;
    4. attaches every owed signal to its party's open case, and raises (-20004) if any is left unattached;
    5. re-types open cases: `CROSS_DOMAIN` at `cross_domain_min_distinct_rules` distinct rule_ids (or rules of two types), otherwise `case_type_by_rule`;
    6. returns the counts.

    Cases are derived from SIGNAL state rather than the batch, so a later run completes anything an earlier one did not.
  - `ROUTE_SIGNALS_TASK` in EVIDENCE_CHAIN: suspended, then `CREATE OR ALTER TASK` on `{{ wh.pipeline }}` with `WHEN SYSTEM$STREAM_HAS_DATA` and no schedule (D38). The body calls the router and passes its counts to `SYSTEM$SET_RETURN_VALUE`, which puts them in TASK_HISTORY. It is resumed only if `resume_route_task`.
- `config.py`:
  - `streams_tasks` is a typed `StreamsTasks` model (a YAML boolean did not fit `dict[str, str]`, and the old name mapper would have turned `false` into `FALSE_DEV`);
  - `_check_routing` refuses: duplicate or empty severities, an unknown `min_severity_for_case`, empty open statuses, a cross-domain minimum that is not an integer >= 2, and a registered rule without a `FRAUD` / `AML` case type;
  - `sql_context` gains `stream_task`, `routing` (typed literals via `policy.routing_context`) and `routed_id`.
- `ids.py`: `signal_id(key)` = `SIG-` + the first 16 hex characters of SHA-256; `case_id(party, opening key)` = `CASE-` + the same over `party|key`. The SQL uses the same prefixes and width from `sql_context`.
- `conf/base.yml`: `route_signals_procedure: ROUTE_SIGNALS`, `route_signals_task: ROUTE_SIGNALS_TASK`, `resume_route_task: false`; `route_signals_schedule` deleted from base, dev and demo. `demo.yml`: `resume_route_task: true`. `conf/detection.yml`: `routing.case_type_by_rule` (`STRUCTURING_CROSS_ACCOUNT: AML`).

**Live, on RISK_COPILOT_DEV:** the stage was deployed twice, 4 statements ok each time; the second deploy exits 0 (the first try failed on `COMMENT` placed after `EXECUTE AS`, which CREATE PROCEDURE does not accept). The task is `suspended` with a NULL schedule, the WHEN on the stream, and the pipeline warehouse. The proof is M31.

**Tests:** 485 passed (460 before). New `tests/unit/test_streams_tasks.py` (5 tests run for dev and demo, plus 12 profile-independent cases):
- the stream is `IF NOT EXISTS ON DYNAMIC TABLE <signals_fast>`, and no stream statement replaces or alters;
- the task is triggered by that stream on the pipeline warehouse, with no SCHEDULE;
- it is suspended before the alter, and resumed iff configured (dev no, demo yes);
- a leftover `route_signals_schedule` fails the load;
- version and severity are read from the registry;
- the SQL derives ids with the `ids.py` prefixes and width;
- changing each routing value changes the rendered procedure;
- 9 routing refusals.

`test_ids.py`: the routed-id formulas, and refusal of empty inputs.

**Departures from the plan**

| Plan said | Built | Why |
|---|---|---|
| "`EXECUTE TASK` twice, the second run adds 0 rows" | `CALL ROUTE_SIGNALS(TRUE)` twice (10, then 0), then `EXECUTE TASK` twice (both SKIPPED, +0) | A new stream is empty (above), so an executed task has nothing to route. The full-scan pair is the stronger test: the same 10 signals are fed twice |
| A task on a schedule (`route_signals_schedule`) | Triggered task, schedule key deleted | D38 |
| One `ROUTE_SIGNALS` name for task and procedure | Procedure `ROUTE_SIGNALS`, task `ROUTE_SIGNALS_TASK` | Two object types sharing one name is ambiguous in history and grants |
| "A case appears for each detected host" | 10 cases, one per host, hero included (M31) | -- |

**Not proven yet:** the stream's delta path and the task's `SYSTEM$SET_RETURN_VALUE` run only when a refresh changes signals. Step 8's replay changes them and records the triggered runs.

**Verification:** 485 passed; ruff, mypy (29 files), lint (dev + demo, 14 templates each) clean; `doctor --profile dev` exit 0, 0 failed.

## Step 7 -- `riskcopilot verify` (obligation 7; D39-D43)

**Found before building** (read-only probe, admin role):
- the 10 signals all sit in bucket 2022-08-26, while all 101 of their evidence legs and all 494 grain rows are in 2022-09. That grain mismatch is the reason for D40;
- the docs state that a manual `ALTER DYNAMIC TABLE ... REFRESH` also refreshes every upstream dynamic table (scheduler enabled), and cannot run inside a transaction.

**What changed**
- New `src/risk_copilot/pipeline/verify.py`. It renders the queries, binds configured values and reads back their verdicts; it compares no count to a bound and reads no label.
  - `read_pin` refuses, with no connection, a pin whose status, profile, seed or must-trigger rules differ from config (D43).
  - `Verifier.run`, as the admin role:
    1. checks the loaded seed against the pin;
    2. refreshes;
    3. checks the signal yield;
    4. computes the metrics;
    5. checks the hero;
    6. writes the sibling result file (D41) and one EVAL_RUN row (D42).

    It never routes (D39).
  - The exit code comes from the SQL's `within_bounds` and `detected` columns. The yield blocks only when `validation.signal_yield.fail_build` is set.
- New `sql/verify/` (rendered from `sql_context`, linted, never deployed):

  | File | What it does |
  |---|---|
  | `01_refresh.sql` | one `ALTER DYNAMIC TABLE ... REFRESH` over the signal tables some registered rule writes to |
  | `02_signal_yield.sql` | registered rules FULL OUTER JOIN the counts, bounds bound as `?`. A rule with 0 signals is listed and fails; an unregistered rule id in a signal table fails |
  | `03_detection_metrics.sql` | predicted vs expected party-months per rule: TP, FP split legit / other typology / not in grain, missed, recall, precision, unresolved evidence, and the control |
  | `04_loaded_seed.sql` | the generation seeds of the loaded injected legs |
  | `05_hero.sql` | per must-trigger rule: detected, signal keys, case ids |
  | `06_eval_run.sql` | the EVAL_RUN insert |
  | `_macros.sql` | the signal tables, their union, and the registered rules, all from the `rule` context |
  | `controls/STRUCTURING_CROSS_ACCOUNT.sql` | party-months with a genuine (label not illicit) in-band cash credit, band and CTR joined through the rule's clause roles |
  | `controls/_none.sql` | the marker row picked by Jinja's include-list fallback when a rule has no control file |
- `generation/eval_grain.py`: `MONTH_BUCKET_FORMAT` / `MONTH_BUCKET_SQL_FORMAT`, the one definition of the grain's month. `config.sql_context()` gains `eval_grain.month_format`.
- `conf/generation.yml`: `output.demo_entity_verified_file`.
- `deploy.py`: `verify_files()`. `lint()` renders the verify queries and lints every included fragment directory (`rules/`, `verify/controls/`) and the verify macros. `lint.py` reports the verify count.
- `cli.py`: the `verify` command. It prints three tables (yield, detection, hero), warns about a hero signal that is not routed yet, and exits 0 / 1 / 2.
- New committed file `conf/generated/demo_entity.dev.verified.yml`.

**Live, on RISK_COPILOT_DEV:** `riskcopilot --profile dev verify` exited 0, twice. The numbers are M32-M35:
- yield 10 in [1, 2000];
- recall and precision 10/10, control 0 of 1 fired;
- hero `CONFIRMED_BY_DETECTION` on `CASE-4399c4c86db5c6ea`, and the result file is byte-identical across the two runs;
- the stream, the evidence chain and the task are unchanged; EVAL_RUN gained 2 rows.

**Tests:** 519 passed (485 before). New `tests/unit/test_verify.py` (34 cases), with a recording fake session:
- **the SQL takes everything from config:**
  - every query is one statement;
  - the rules and signal tables come from config, and moving a rule's `dynamic_table` moves every query;
  - the yield bounds are bound (1/2000 dev, 5/2000 demo) and never rendered;
  - the control reads band and CTR through the rule's clause roles;
  - a rule without a control file reports none;
  - the evidence-month mapping uses the grain's format, the Python and SQL formats agree, and changing the format changes the SQL;
  - lint covers the verify queries.
- **Python decides nothing:**
  - the module's code names no label, grain, clause or threshold, and compares no count;
  - the exit code follows the SQL's verdict even where Python's arithmetic would disagree (both directions);
  - an unregistered rule fails;
  - a non-blocking gate reports without failing;
  - the SQL lists a zero-signal rule.
- **The hero:**
  - confirmed beside the pin, whose bytes are unchanged;
  - rejected with exit 1 and `passed` false in EVAL_RUN;
  - an unrouted signal is confirmed with a warning;
  - the result file is byte-identical for the same data;
  - 5 kinds of stale pin are refused, as is a missing pin;
  - data from another seed is refused before the refresh, and nothing is written;
  - the CLI exits 2 on a stale pin without opening a session.
- **What verify writes:**
  - no CALL, EXECUTE TASK, RESUME, MERGE, DELETE or UPDATE, and no stream or router name;
  - exactly two writes, the refresh and EVAL_RUN;
  - the EVAL_RUN row carries the run.

`test_policy_seed.py`: the "no threshold outside the seed" check now covers the verify queries too.

**Departures from the plan**

| Plan said | Built | Why |
|---|---|---|
| "The hero pin becomes CONFIRMED_BY_DETECTION" | The pin is untouched; the result is a sibling file | D41 |
| Refresh `DT_SIGNALS_FAST` | Refresh every signal table a registered rule writes to (today the same one table) | So a rule assigned to `signals_aggregate` is refreshed without a code change |
| "The one genuine in-band deposit is the false-positive control" | A per-rule control query, reported as "N of M fired" | The control is rule-specific; the file makes it explicit and optional (D43) |
| -- | Loaded-seed check, exit 2 | D43: not in the plan; found when asking what "confirmed" would be confirming |
| -- | One EVAL_RUN row per run | D42 |

**Verification:** 519 passed; ruff, mypy (30 files), lint (dev + demo, 14 templates and 6 verify queries each) clean; `doctor --profile dev` exit 0, 0 failed, 14 warnings.
