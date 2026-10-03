# Risk Copilot: Agent Design, Architecture, and Deployment Guide

> **What this document is:** A complete explanation of how the AI agent works, how it
> connects to the generated database, how it runs in the cloud (inside Snowflake --
> no external hosting), how each piece is built, why each piece exists, and how they
> all orchestrate together into the final product.
>
> **Who this is for:** Someone who has the generated database already running and wants
> to understand the full picture before touching any code.

> **As built (Oct 2026): the agent runs in a second Snowflake account.** The data
> account (`HTQQLHX-GI22223`, `RISK_COPILOT_DEV`) is a trial with no `AI_COMPLETE`, so the
> agentic layer is deployed in `HQRWRXK-UZ26728` as `RISK_COPILOT_AGENT.STR.RC_STR_AGENT`
> (`agent_account/`). The two accounts are in different organisations and regions, so
> neither data sharing nor external access is available; the bridge
> (`src/risk_copilot/agentic`, used by the Slack bot and the case page's
> "4 · AI STR + PDF" button) holds one key-pair connection to each:
>
> 1. data account: current draft or `DRAFT_STR` (deterministic, hashed evidence) + 2 Cortex Analyst questions
> 2. agent account: hashed `EVIDENCE_PACKAGE` -> agent: `draft_str_narrative` (claude-sonnet-4-6)
>    -> `review_str_draft` (openai-gpt-5.1, plus every amount/count/date traced to the evidence;
>    fails closed) -> one redraft on REJECT
> 3. data account, only if approved: `SUBMIT_AI_DRAFT` writes FINDING v(n+1) + ARTIFACT with the
>    figures unchanged, then `REVIEW_STR` re-checks it deterministically
> 4. agent account: `RENDER_STR_PDF` (FIU-IND layout, evidence trail, both verdicts) -> Slack / app
>
> The human gate is unchanged: only the Principal Officer attests (`ATTEST_STR`). Setup and
> commands: `bridge/SLACK_SETUP.md`. Sections below describe the original single-account design.

---

## Table of Contents

1. [The Big Picture -- What Are We Building?](#1-the-big-picture)
2. [How the Generated Data Gets Into Snowflake](#2-how-generated-data-gets-into-snowflake)
3. [What Is a Dynamic Table and Why Do We Need Them?](#3-what-is-a-dynamic-table)
4. [The Automated Detection Pipeline (No Human Needed)](#4-the-automated-detection-pipeline)
5. [The Gap the Agent Fills](#5-the-gap-the-agent-fills)
6. [How the Agent Works Inside Snowflake](#6-how-the-agent-works-inside-snowflake)
7. [How the STR Agent Uses Dynamic Tables (The Connection)](#7-how-the-str-agent-uses-dynamic-tables)
8. [Task-by-Task: What We Build and Why](#8-task-by-task)
9. [How All the Pieces Orchestrate Together](#9-orchestration)
10. [Cloud Architecture -- Why No External Hosting](#10-cloud-architecture)
11. [Security and the Human Gate](#11-security)
12. [One-Command Deploy -- How It Actually Runs](#12-one-command-deploy)
13. [Talking to the Agent -- Your Endpoints](#13-endpoints)
14. [Design Decision Log](#14-design-decisions)

---

## 1. The Big Picture

We are building an **AI-powered compliance assistant** for Indian banks. It does two things:

1. **Detects** suspicious transactions automatically (structuring, mule rings, sanctions hits)
2. **Generates** Suspicious Transaction Reports (STRs) that analysts file with FIU-IND under PMLA 2002

The entire system runs inside Snowflake. There is no web server, no Docker container, no
Vercel app, no AWS Lambda. Snowflake is the database, the compute engine, the AI runtime,
and the API endpoint -- all in one.

Here is the complete system at a glance:

```
+------------------------------------------------------------------+
|                        SNOWFLAKE ACCOUNT                         |
|                                                                  |
|  +-------------------+    +-------------------+                  |
|  | riskcopilot       |    | riskcopilot       |                  |
|  | generate          |--->| load              |                  |
|  | (your laptop)     |    | (puts data in SF) |                  |
|  +-------------------+    +--------+----------+                  |
|                                    |                             |
|                                    v                             |
|  +------------------------------------------------------------+ |
|  |  LAYER 1: RAW                                              | |
|  |  5M+ transaction legs, 2851 parties, 3170 accounts         | |
|  +------------------------------+-----------------------------+ |
|                                 |                                |
|                      (Dynamic Tables -- automatic)               |
|                                 |                                |
|  +------------------------------------------------------------+ |
|  |  LAYER 2: CURATED                                          | |
|  |  DT_DIM_PARTY, DT_TRANSACTIONS_ENRICHED, DT_SIGNALS_FAST  | |
|  +------------------------------+-----------------------------+ |
|                                 |                                |
|                      (Stream + Triggered Task -- automatic)      |
|                                 |                                |
|  +------------------------------------------------------------+ |
|  |  LAYER 3: EVIDENCE_CHAIN                                   | |
|  |  SIGNAL (10), CASE_RECORD (10), CASE_SIGNAL (10)           | |
|  +------------------------------+-----------------------------+ |
|                                 |                                |
|                      (Agent -- when analyst asks)                |
|                                 |                                |
|  +------------------------------------------------------------+ |
|  |  LAYER 4: THE AGENT                                        | |
|  |  Collects evidence --> Generates STR --> Routes to MLRO     | |
|  +------------------------------------------------------------+ |
|                                                                  |
|  Queryable via: Snowsight UI / SQL / REST API / Python           |
+------------------------------------------------------------------+
```

**Layer 1** is data at rest. **Layer 2** is automatic computation. **Layer 3** is automatic
routing. **Layer 4** is the AI agent that reads from all three layers when asked.

---

## 2. How Generated Data Gets Into Snowflake

The `riskcopilot generate` command takes raw academic data (AMLworld HI-Small -- 5 million
financial transactions with known money laundering labels) and transforms it into realistic
Indian banking data:

- Names become Indian names (en_IN Faker): "Rajesh Kumar", "Sunita Devi"
- Amounts are in INR with realistic distributions
- Channels are Indian payment rails: RTGS, NEFT, SWIFT, UPI
- Branches are in Delhi, Mumbai, Pune
- 10 parties have planted structuring patterns (cash deposits just below Rs 10 lakh)
- One "hero" party triggers every detection rule (for demo purposes)

The output is Parquet files with a SHA-256 manifest (byte-identical per seed -- the same
seed always produces the same data).

The `riskcopilot load` command then does a single-transaction load into Snowflake:

```
Your laptop                         Snowflake
----------                          ---------
parties.parquet        ----COPY---> RAW.RAW_PARTY
accounts.parquet       ----COPY---> RAW.RAW_ACCOUNT
transactions.parquet   ----COPY---> RAW.RAW_AMLWORLD_TRANS  (5,078,345 rows)
relationships.parquet  ----COPY---> RAW.RAW_PARTY_RELATIONSHIP
profiles.parquet       ----COPY---> RAW.RAW_DECLARED_PROFILE
labels.parquet         ----COPY---> GROUND_TRUTH.GROUND_TRUTH_STAGE  (isolated)
```

The load runs in **one transaction** -- if any table fails, everything rolls back. After
the load, the raw tables contain the full generated dataset, and everything downstream is
automatic.

**Why this matters for the agent:** The agent never reads RAW directly. It reads from
CURATED and EVIDENCE_CHAIN, which are computed from RAW by dynamic tables. But the data
the agent ultimately presents in an STR -- the transaction amounts, party names, account
numbers -- all originated from this generated dataset.

---

## 3. What Is a Dynamic Table and Why Do We Need Them?

### The problem dynamic tables solve

After `riskcopilot load`, you have 5 million raw transaction rows sitting in `RAW.RAW_AMLWORLD_TRANS`. Each row is a single transaction leg -- a debit or a credit. It looks like this:

```
txn_id       | debit_account | credit_account | amount  | timestamp           | is_cash
-------------|---------------|----------------|---------|---------------------|--------
TXN-00000001 | A-000000042   | A-000000099    | 950000  | 2022-09-05 10:23:00 | true
TXN-00000002 | A-000000042   | A-000000101    | 980000  | 2022-09-07 14:15:00 | true
TXN-00000003 | A-000000042   | A-000000101    | 970000  | 2022-09-09 09:47:00 | true
... 5 million more rows ...
```

But to detect structuring, you don't want raw rows. You need to ask questions like:

- "Which parties deposited cash into 2+ different accounts within 7 days?"
- "Were those deposits between 90% and 99.9% of Rs 10 lakh?"
- "What is this party's name, KYC status, and branch?"

Answering these questions requires **joining** multiple tables (transactions + parties + accounts + policy clauses), **aggregating** (group by party and time window), and **filtering** (only cash deposits in the structuring band). You could write a SQL query that does all of this, but:

- It would take minutes to run on 5 million rows
- You'd have to run it manually every time new data arrives
- The results would be stale the moment new transactions come in

### What a dynamic table actually is

A dynamic table is a **query result that Snowflake keeps up to date for you**. You write the query once (as a `CREATE DYNAMIC TABLE` statement), and Snowflake re-runs it automatically on a schedule you specify.

Think of it like a spreadsheet formula. If cell A1 has `=SUM(B1:B100)`, you don't re-type the formula every time you change a value in column B. The spreadsheet recalculates automatically. A dynamic table is the same idea, but for millions of database rows.

Here is a simplified example:

```sql
-- This is NOT a view (which re-runs every time you query it).
-- This is NOT a materialized view (which has restrictions on what SQL you can use).
-- This is a DYNAMIC TABLE: Snowflake stores the result AND keeps it fresh.

CREATE DYNAMIC TABLE DT_SIGNALS_FAST
    TARGET_LAG = '5 minutes'        -- "keep this no more than 5 minutes stale"
    WAREHOUSE = RC_PIPELINE_WH      -- "use this warehouse to refresh"
    REFRESH_MODE = INCREMENTAL      -- "only process what changed, not everything"
AS
SELECT
    party_id,
    COUNT(DISTINCT account_id) AS accounts_used,
    SUM(amount) AS total_deposited,
    ARRAY_AGG(txn_id) AS evidence_txn_ids
FROM transactions_enriched
WHERE is_cash = TRUE
  AND amount BETWEEN 900000 AND 999900      -- structuring band
GROUP BY party_id, time_window
HAVING COUNT(DISTINCT account_id) >= 2;     -- 2+ accounts = suspicious
```

After this runs, `DT_SIGNALS_FAST` is a table you can SELECT from like any other table. But unlike a regular table, **you never INSERT into it**. Snowflake populates it by running the query, and re-runs it every 5 minutes (or sooner if something downstream needs it).

### The dynamic tables in this project

We have 5 dynamic tables, each one transforming data for the next:

```
RAW.RAW_PARTY + RAW.RAW_ACCOUNT
    |
    v
DT_DIM_PARTY
    What it does:  Joins party + account + KYC into one row per party
    Why we need it: The agent needs "Rajesh Kumar, PAN ABCPK1234F, branch Delhi"
                    in one query, not scattered across 3 raw tables
    Refreshes:     On demand (only when something reads it)
    Row count:     ~2,851 parties


RAW.RAW_AMLWORLD_TRANS + DT_DIM_PARTY
    |
    v
DT_TRANSACTIONS_ENRICHED
    What it does:  Joins each transaction with its party names, account types,
                   currency rates, and flags (is_cash, is_cross_border, is_internal)
    Why we need it: Raw transactions don't have party names or cash flags.
                    Every downstream table needs these enriched fields.
    Refreshes:     Every 1 minute
    Row count:     ~5 million enriched legs


DT_TRANSACTIONS_ENRICHED
    |
    v
DT_TXN_DAILY_AGG
    What it does:  Summarizes each party's activity per day: total credits,
                   total debits, cash amount, distinct counterparties
    Why we need it: Rolling window features (7-day, 30-day) are built from
                    daily buckets, not from scanning every individual transaction
    Refreshes:     Every 5 minutes
    Row count:     ~50,000 party-day rows


DT_TRANSACTIONS_ENRICHED + GOVERNANCE.V_POLICY_CLAUSE_CURRENT
    |
    v
DT_SIGNALS_FAST
    What it does:  THE DETECTION ENGINE. Runs the structuring rule:
                   finds parties with 2+ accounts receiving cash deposits
                   between 90-99.9% of the CTR threshold within 7 days
    Why we need it: This is where suspicion is born. Without this table,
                    nobody knows which parties are structuring.
    Refreshes:     Every 5 minutes
    Row count:     10 signals (on the generated data)


DT_TRANSACTIONS_ENRICHED
    |
    v
DT_PARTY_RELATIONSHIP
    What it does:  Computes fund flow edges and shared-device relationships
                   from transaction patterns
    Why we need it: Mule ring detection requires knowing who sends money to whom
    Refreshes:     Every 60 minutes
    Row count:     ~15,000 relationship edges
```

### Why dynamic tables instead of views or scheduled jobs

| Approach | Problem |
|----------|---------|
| Regular view | Re-runs the query every time you SELECT from it. On 5M rows, that's slow. The agent would wait minutes for each case lookup. |
| Materialized view | Snowflake restricts what SQL you can put in a materialized view: no joins to other materialized views, limited aggregation. Our detection query is too complex. |
| Scheduled stored procedure | You have to write the refresh logic yourself, handle failures, manage state. If the procedure crashes halfway, you have partial results. |
| **Dynamic table** | Snowflake manages everything: scheduling, incremental refresh, failure retry, dependency ordering. You just write the query. The result is always fresh (within the target lag). |

The `REFRESH_MODE = INCREMENTAL` part is important. It means Snowflake doesn't re-scan all 5 million rows on every refresh. It only processes the rows that changed since the last refresh. On a system where new transactions arrive continuously, this keeps refresh times in seconds, not minutes.

### How dynamic tables chain together

Dynamic tables can read from other dynamic tables. Snowflake understands the dependency graph and refreshes them in the right order:

```
DT_DIM_PARTY (refreshes first, on demand)
    |
    v
DT_TRANSACTIONS_ENRICHED (refreshes second, every 1 minute)
    |
    +---> DT_TXN_DAILY_AGG (refreshes third, every 5 minutes)
    |
    +---> DT_SIGNALS_FAST (refreshes third, every 5 minutes)
    |
    +---> DT_PARTY_RELATIONSHIP (refreshes third, every 60 minutes)
```

Snowflake guarantees: DT_SIGNALS_FAST will never refresh before DT_TRANSACTIONS_ENRICHED has finished its refresh. The data is always consistent across the chain.

The project's config system (`conf/base.yml`) validates this dependency graph at load time. If you accidentally set DT_DIM_PARTY's lag to 1 day but DT_TRANSACTIONS_ENRICHED's lag to 1 minute, the config loader refuses to start — because Snowflake would reject a table whose lag is shorter than its upstream's lag.

---

## 7. How the STR Agent Uses Dynamic Tables (The Connection)

This is the part that connects everything together. Let's trace a concrete example.

### The scenario

Party `P-000000000042` (Rajesh Kumar) made three cash deposits:
- Rs 9,50,000 on Sept 5 into account A-000000042
- Rs 9,80,000 on Sept 7 into account A-000000101
- Rs 9,70,000 on Sept 9 into account A-000000101

All three are between 90-99.9% of Rs 10 lakh (the CTR threshold). Two different accounts were used within 7 days. This is textbook structuring.

### Step 1: DT_SIGNALS_FAST detects it

When the dynamic table refreshes, the structuring rule finds these three transactions. It produces one signal row:

```
rule_id:              STRUCTURING_CROSS_ACCOUNT
party_id:             P-000000000042
metric_value:         2                    (2 distinct accounts)
threshold_value:      2                    (minimum from POLICY_CLAUSE)
evidence_txn_ids:     [TXN-00000001, TXN-00000002, TXN-00000003]
signal_business_key:  STRUCTURING_CROSS_ACCOUNT|P-000000000042|2022-09-05
```

**Notice `evidence_txn_ids`** — the signal carries the exact transaction IDs that caused it. This is how the agent will later find the actual amounts and dates.

### Step 2: The router persists and deduplicates

ROUTE_SIGNALS reads this signal from the stream and MERGEs it into the SIGNAL table. It also creates a CASE_RECORD for this party and a CASE_SIGNAL link.

After routing:
```
SIGNAL:       signal_id = SIG-a1b2c3d4e5f6a1b2
              (the SHA-256 hash of the business key)
CASE_RECORD:  case_id = CASE-4399c4c86db5c6ea
              party_id = P-000000000042
              case_type = AML
              branch_region = DELHI
CASE_SIGNAL:  links the signal to the case
```

### Step 3: Analyst asks the agent

The analyst types: "Generate STR for case CASE-4399c4c86db5c6ea"

### Step 4: The agent calls COLLECT_CASE_EVIDENCE

This procedure reads from the dynamic tables:

**Read 1: CASE_RECORD** (not a dynamic table, but populated by the router)
```sql
SELECT party_id, case_type, branch_region
FROM EVIDENCE_CHAIN.CASE_RECORD
WHERE case_id = 'CASE-4399c4c86db5c6ea';
-- Returns: P-000000000042, AML, DELHI
```

**Read 2: SIGNAL via CASE_SIGNAL** (not a dynamic table)
```sql
SELECT s.rule_id, s.severity, s.evidence_txn_ids, s.metric_value, s.threshold_value
FROM EVIDENCE_CHAIN.SIGNAL s
JOIN EVIDENCE_CHAIN.CASE_SIGNAL cs ON cs.signal_id = s.signal_id
WHERE cs.case_id = 'CASE-4399c4c86db5c6ea';
-- Returns: STRUCTURING_CROSS_ACCOUNT, HIGH, [TXN-001, TXN-002, TXN-003], 2, 2
```

**Read 3: DT_DIM_PARTY** (DYNAMIC TABLE -- this is the connection)
```sql
SELECT full_name, party_type, tax_id, nationality, kyc_status, risk_rating, branch_region
FROM CURATED.DT_DIM_PARTY
WHERE party_id = 'P-000000000042';
-- Returns: Rajesh Kumar, INDIVIDUAL, ABCPK1234F, IND, VERIFIED, LOW, DELHI
```

Without DT_DIM_PARTY, the agent would have to join RAW_PARTY + RAW_ACCOUNT + RAW_KYC manually. The dynamic table has already done that join and keeps it fresh.

**Read 4: DT_TRANSACTIONS_ENRICHED** (DYNAMIC TABLE -- this is the connection)
```sql
SELECT txn_id, txn_date, amount_inr, channel, debit_party_name, credit_party_name, is_cash
FROM CURATED.DT_TRANSACTIONS_ENRICHED
WHERE txn_id IN ('TXN-00000001', 'TXN-00000002', 'TXN-00000003');
-- Returns:
-- TXN-00000001 | 2022-09-05 | 950000 | NEFT | Cash Desk Delhi | Rajesh Kumar | true
-- TXN-00000002 | 2022-09-07 | 980000 | NEFT | Cash Desk Delhi | Rajesh Kumar | true
-- TXN-00000003 | 2022-09-09 | 970000 | NEFT | Cash Desk Delhi | Rajesh Kumar | true
```

Without DT_TRANSACTIONS_ENRICHED, the agent would have to join RAW_AMLWORLD_TRANS with the party tables to get names, channels, and cash flags. The dynamic table has already done this.

**Read 5: V_POLICY_CLAUSE_CURRENT** (a view on GOVERNANCE.POLICY_CLAUSE)
```sql
SELECT clause_id, document_title, clause_ref, threshold_lower
FROM GOVERNANCE.V_POLICY_CLAUSE_CURRENT
WHERE clause_id = 'CTR_CASH_THRESHOLD';
-- Returns: CTR_CASH_THRESHOLD, PML Rules 2005, Rule 3(1)(A), 1000000
```

### Step 5: The agent assembles the JSON

COLLECT_CASE_EVIDENCE combines all five reads into one JSON object:

```json
{
  "subject_name": "Rajesh Kumar",
  "subject_type": "INDIVIDUAL",
  "subject_id": "ABCPK1234F",
  "accounts": [
    {"account_number": "A-000000042", "type": "CURRENT", "branch": "DELHI"},
    {"account_number": "A-000000101", "type": "SAVINGS", "branch": "DELHI"}
  ],
  "transactions": [
    {"date": "2022-09-05", "amount": 950000, "channel": "NEFT", "description": "Cash deposit"},
    {"date": "2022-09-07", "amount": 980000, "channel": "NEFT", "description": "Cash deposit"},
    {"date": "2022-09-09", "amount": 970000, "channel": "NEFT", "description": "Cash deposit"}
  ],
  "total_amount": 2900000,
  "suspicion_codes": ["D", "F"],
  "suspicion_summary": "3 cash deposits between Rs 9.0L-9.99L into 2 accounts within 5 days",
  "typology": "STRUCTURING",
  "policy_clauses_cited": ["CTR_CASH_THRESHOLD", "STRUCTURING_BAND"]
}
```

### Step 6: The agent calls GENERATE_STR

This JSON goes to GENERATE_STR, which calls the LLM. The LLM writes formal prose around these concrete numbers:

> **SUSPICIOUS TRANSACTION REPORT (DRAFT)**
>
> Subject Rajesh Kumar (PAN: ABCPK1234F) made three cash deposits totalling
> Rs 29,00,000 into two accounts at the Ashok Vihar branch between 5 September
> and 9 September 2022. Each deposit (Rs 9,50,000, Rs 9,80,000, and Rs 9,70,000)
> fell within the 90-99.9% band of the statutory CTR threshold of Rs 10,00,000
> under PMLA Rule 3(1)(A)...

### The summary: where each dynamic table helps

```
Dynamic Table               What the agent gets from it
--------------------------  ---------------------------------------------
DT_DIM_PARTY                Party name, PAN, KYC status, branch
                            (the "Subject Information" section of the STR)

DT_TRANSACTIONS_ENRICHED    Transaction dates, amounts, channels, counterparties
                            (the "Suspicious Transactions" section of the STR)

DT_TXN_DAILY_AGG            Daily activity summaries
                            (used by velocity features, not directly by the agent)

DT_SIGNALS_FAST             The detection itself -- which party, which rule,
                            which transactions caused it
                            (the "Grounds of Suspicion" section of the STR)

DT_PARTY_RELATIONSHIP       Fund flow and shared-device edges
                            (the "Related Parties" section of the STR)
```

Without dynamic tables, the agent would have to run these heavy joins and aggregations every time an analyst asks a question. With dynamic tables, the work is already done -- the agent just reads the pre-computed, fresh results.

---

## 4. The Automated Detection Pipeline

Once data is in RAW, Snowflake takes over. No cron jobs, no Airflow, no scheduler. Dynamic
tables refresh themselves.

### 3a. Dynamic Tables (CURATED schema)

```
RAW.RAW_PARTY ----+
RAW.RAW_ACCOUNT --+--> DT_DIM_PARTY
                       (joins party + account + KYC into one row per party)
                       (refreshes: on demand, when something reads it)
                            |
RAW.RAW_AMLWORLD_TRANS -----+--> DT_TRANSACTIONS_ENRICHED
                                 (joins each transaction leg with its party
                                  and account, adds FX rates, flags cash/
                                  cross-border, resolves internal accounts)
                                 (refreshes: every 1 minute)
                                      |
                                      +--> DT_TXN_DAILY_AGG
                                      |    (daily summary per party: total credits,
                                      |     debits, cash amounts, distinct counterparties)
                                      |    (refreshes: every 5 minutes)
                                      |
                                      +--> V_TXN_VELOCITY_FEATURES (view)
                                      |    (rolling 7d/30d/90d windows computed
                                      |     from the daily aggregates)
                                      |
                                      +--> DT_SIGNALS_FAST
                                           (THE DETECTION ENGINE -- see below)
                                           (refreshes: every 5 minutes)
```

### 3b. How DT_SIGNALS_FAST Detects Structuring

`DT_SIGNALS_FAST` is the dynamic table that runs the actual detection rules. For the
structuring rule (`STRUCTURING_CROSS_ACCOUNT`), here is what happens on every refresh:

1. It reads all cash credit legs from `DT_TRANSACTIONS_ENRICHED`
2. It joins to `GOVERNANCE.V_POLICY_CLAUSE_CURRENT` to get the **live thresholds**:
   - CTR threshold: Rs 10,00,000 (from PMLA Rule 3(1)(A))
   - Structuring band: 90-99.9% of CTR threshold
   - Minimum accounts: 2 distinct accounts
   - Window: 7 days
3. It filters to deposits between Rs 9,00,000 and Rs 9,99,900
4. It groups by (party, 7-day window) and checks: did this party deposit into 2+ accounts?
5. If yes: a signal row is emitted

**The key point:** The thresholds (Rs 10 lakh, 90%, 2 accounts, 7 days) are NOT hardcoded
in the SQL. They come from `GOVERNANCE.POLICY_CLAUSE`, which is seeded from
`conf/detection.yml`. If the regulator changes the threshold, you change the YAML, redeploy
the policy stage, and the next DT refresh picks it up. No code change.

After the refresh, `DT_SIGNALS_FAST` contains 10 rows -- one signal per planted
structuring host. Each signal carries:
- `rule_id`: which rule fired (STRUCTURING_CROSS_ACCOUNT)
- `party_id`: which party triggered it
- `metric_value`: how many accounts (the observed value)
- `threshold_value`: the minimum required (from policy)
- `evidence_txn_ids`: an ARRAY of the exact transaction IDs that caused it
- `signal_business_key`: a unique key for deduplication

### 3c. The Stream and Triggered Task (Routing)

Dynamic tables recompute their full result on every refresh. Without deduplication, the same
10 signals would be inserted into SIGNAL every 5 minutes, creating thousands of duplicate
cases. This is solved by a three-part routing system:

```
DT_SIGNALS_FAST
    |
    | (Snowflake stream -- captures changes)
    v
SIGNALS_STREAM
    |
    | (when stream has data, task fires automatically)
    v
ROUTE_SIGNALS_TASK
    |
    | (calls the router procedure)
    v
ROUTE_SIGNALS(full_scan BOOLEAN)
    |
    | One transaction: all or nothing
    |
    +---> SIGNAL table        (MERGE on signal_business_key -- no duplicates)
    +---> CASE_RECORD table   (one case per party that needs one)
    +---> CASE_SIGNAL table   (links signals to their case)
```

The router runs in a single database transaction. If anything fails, everything rolls back
and the stream is unconsumed -- the next run retries the same data.

**After routing completes**, the EVIDENCE_CHAIN schema contains:
- 10 SIGNAL rows (deduplicated -- running the router again adds 0 rows)
- 10 CASE_RECORD rows (one per structuring host, case_type = 'AML')
- 10 CASE_SIGNAL rows (linking each signal to its case)

**This is where the pipeline hands off to the agent.** Everything up to here is fully
automatic. The agent enters the picture when an analyst asks about a case.

---

## 5. The Gap the Agent Fills

Look at the EVIDENCE_CHAIN schema after the pipeline runs:

```
Tables that ARE populated (by the pipeline):
  SIGNAL .................. 10 rows (detection results)
  CASE_RECORD ............. 10 rows (one per suspicious party)
  CASE_SIGNAL ............. 10 rows (signal-to-case links)

Tables that are EMPTY (the agent fills these):
  EVIDENCE ................ 0 rows  <-- pointers to source data
  EVIDENCE_QUERY_LOG ...... 0 rows  <-- reproducibility log
  FINDING ................. 0 rows  <-- the STR narrative
  REVIEW .................. 0 rows  <-- adversarial validation
  ARTIFACT ................ 0 rows  <-- the formatted STR document
  ATTESTATION ............. 0 rows  <-- human sign-off
```

The gap is the journey from "we detected something suspicious" to "we have a filed STR".
That journey has 6 steps, and the agent automates the first 4:

```
Step 1: COLLECT EVIDENCE ........ Agent reads SIGNAL, CASE, PARTY, TRANSACTIONS
                                  and writes to EVIDENCE + EVIDENCE_QUERY_LOG
                                  (automated by collect_case_evidence procedure)

Step 2: DRAFT THE STR ........... Agent calls the LLM to write the narrative
                                  and writes to FINDING + STR_OUTPUT
                                  (automated by generate_str procedure)

Step 3: ADVERSARIAL REVIEW ...... A different AI model checks the draft
                                  and writes to REVIEW
                                  (automated by review_finding procedure)

Step 4: PACKAGE FOR SIGNING ..... Agent creates the formal ARTIFACT
                                  and writes to ARTIFACT
                                  (automated by request_attestation procedure)

Step 5: HUMAN SIGN-OFF .......... MLRO reviews and signs
                                  and writes to ATTESTATION
                                  >>> THIS STEP IS NEVER AUTOMATED <<<
                                  >>> ONLY COMPLIANCE_OFFICER ROLE CAN DO IT <<<

Step 6: FILE WITH FIU-IND ....... Submission to the regulator
                                  >>> NOT IN SCOPE -- external system <<<
```

---

## 6. How the Agent Works Inside Snowflake

### 5a. What IS a Snowflake Cortex Agent?

A Cortex Agent is a Snowflake object, like a table or a view. You create it with SQL:

```sql
CREATE AGENT my_agent FROM SPECIFICATION $$ ... $$;
```

It is stored in a schema, has an owner, and can be granted to roles. Snowflake runs it on
its own infrastructure. You do not deploy it to a server.

The agent has three parts:

1. **Instructions** -- A system prompt that tells the AI who it is and how to behave
2. **Tools** -- Procedures and functions the AI can call (database operations)
3. **Budget** -- Time and token limits per conversation turn

When a user sends a message, Snowflake's orchestration model reads the instructions,
looks at the available tools, and decides which tool to call. The tool runs a stored
procedure on a warehouse. The procedure's result goes back to the orchestration model,
which formulates the response.

```
User message: "Generate STR for case CASE-xxx"
        |
        v
+-------------------+
| Orchestration     |  (Snowflake's built-in model)
| Model             |  Reads: instructions, tool descriptions
|                   |  Decides: "I should call collect_case_evidence first"
+--------+----------+
         |
         | Tool call: collect_case_evidence(case_id='CASE-xxx')
         v
+-------------------+
| Warehouse         |  RC_DEMO_WH runs the SQL
| (XSMALL)          |  Reads: SIGNAL, CASE_RECORD, DT_DIM_PARTY,
|                   |         DT_TRANSACTIONS_ENRICHED, V_POLICY_CLAUSE_CURRENT
+--------+----------+
         |
         | Returns: JSON with party, transactions, signals, clauses
         v
+-------------------+
| Orchestration     |  Decides: "Now I should call generate_str with this JSON"
| Model             |
+--------+----------+
         |
         | Tool call: generate_str(input_json='{...}')
         v
+-------------------+
| Warehouse         |  RC_DEMO_WH runs:
| (XSMALL)          |  1. VALIDATE_STR_INPUTS (checks fields)
|                   |  2. INSERT INTO STR_INPUT (audit trail)
|                   |  3. CORTEX.COMPLETE('claude-sonnet-4', prompt)  <-- THE LLM CALL
|                   |  4. INSERT INTO STR_OUTPUT (stores the draft)
+--------+----------+
         |
         | Returns: { status: SUCCESS, narrative: "...", str_output_id: "..." }
         v
+-------------------+
| Orchestration     |  Formats the response for the user
| Model             |  "Here is your STR draft: ..."
+-------------------+
         |
         v
User sees the STR draft
```

### 5b. Where Does the LLM Call Happen?

The LLM call is `SNOWFLAKE.CORTEX.COMPLETE('claude-sonnet-4', prompt)`. This is a built-in
Snowflake function. It:

- Runs inside Snowflake's infrastructure (not an external API call you manage)
- Is billed as Snowflake credits (not a separate OpenAI/Anthropic bill)
- Does not send your data outside Snowflake's security boundary
- Supports multiple model families (Claude, GPT, Llama, Mistral)

The prompt sent to the LLM contains the assembled evidence (party name, transactions,
amounts, policy clauses) and instructions to write a formal STR narrative. The LLM writes
prose. It does NOT compute numbers -- all figures (amounts, thresholds, counts) come from
the SQL queries that ran before the LLM call.

---

## 8. Task-by-Task: What We Build and Why

### Task 1: The Evidence Collector

**What:** A stored procedure called `COLLECT_CASE_EVIDENCE(case_id VARCHAR)`

**Why it exists:** Today, the analyst has to manually look up the case, find its signals,
look up each transaction by ID, find the party's KYC data, and copy-paste it all into a
JSON payload. This procedure does all of that automatically.

**Where it fits in the architecture:**

```
                        THE BRIDGE
                    +------------------+
  Pipeline output   |  COLLECT_CASE_   |   Agent input
  (already exists)  |  EVIDENCE        |   (ready for LLM)
                    |                  |
  CASE_RECORD ------+-> reads case     |
  CASE_SIGNAL ------+-> finds signals  +-----> JSON payload
  SIGNAL -----------+-> gets evidence  |       compatible with
  DT_DIM_PARTY -----+-> gets KYC data  |       GENERATE_STR
  DT_TRANSACTIONS --+-> gets txns      |
  V_POLICY_CLAUSE --+-> gets rules     |
                    +------------------+
```

**How it works:**

1. Takes a `case_id` as input
2. Looks up the case in `CASE_RECORD` -- gets `party_id`, `case_type`, `branch_region`
3. Finds all signals via `CASE_SIGNAL` join `SIGNAL` -- gets `rule_id`, `severity`,
   `evidence_txn_ids` (the array of transaction IDs that caused each signal)
4. Reads the party from `DT_DIM_PARTY` -- gets name, PAN, nationality, KYC status
5. Reads the actual transactions from `DT_TRANSACTIONS_ENRICHED` using the evidence
   transaction IDs -- gets dates, amounts, channels, counterparties
6. Reads the policy clauses from `V_POLICY_CLAUSE_CURRENT` -- gets the regulatory
   references (PMLA Rule 3(1)(A), etc.)
7. Inserts EVIDENCE pointer rows (not copies -- just references with timestamps)
8. Returns everything as a single JSON object

**How it connects to the generated data:** The transaction IDs in `evidence_txn_ids`
were assigned during `riskcopilot generate` and loaded during `riskcopilot load`. They
trace all the way back to specific AMLworld pairs. When the evidence collector reads
transaction TXN-xxx from `DT_TRANSACTIONS_ENRICHED`, that row contains the amount,
channel, and parties that were generated from the source data.

### Task 2: The STR Generator (already exists, gets enhanced)

**What:** A stored procedure called `GENERATE_STR(input_json VARCHAR)`

**Why it exists:** Takes the assembled evidence JSON and produces a formal STR narrative
using an LLM. This procedure already exists in `sql/agents/str_generator_agent.sql`.

**Enhancement:** Currently it only accepts raw JSON that the user assembles manually.
We add the ability to accept a `case_id` and call `COLLECT_CASE_EVIDENCE` internally,
so the user can say "generate STR for case X" without assembling any JSON.

**How it works:**

```
Input JSON
    |
    v
1. PARSE_JSON + VALIDATE_STR_INPUTS
   (rejects if subject_name, suspicion_codes,
    suspicion_summary, transactions, or accounts
    are missing)
    |
    v
2. INSERT INTO STR_INPUT
   (audit trail -- every request is recorded)
    |
    v
3. Build the prompt:
   "You are a compliance officer generating an STR
    for FIU-IND under PMLA 2002. Sections: Report
    Details, Subject Info, Accounts, Transactions,
    Related Parties, Grounds of Suspicion, Policy
    Citations, Recommendation. Use ONLY the data
    provided. Do NOT invent facts."
   + the evidence JSON
    |
    v
4. SNOWFLAKE.CORTEX.COMPLETE('claude-sonnet-4', prompt)
   (the actual LLM call -- runs inside Snowflake)
    |
    v
5. INSERT INTO STR_OUTPUT
   (stores the narrative + metadata)
    |
    v
6. RETURN { status, narrative, str_output_id, next_steps }
```

**Why the numbers in the STR are trustworthy:** The prompt receives concrete numbers
(Rs 9,50,000, Rs 9,80,000) that came from SQL queries against the actual transaction
table. The LLM writes prose around those numbers but cannot change them. The STR_INPUT
record preserves exactly what the LLM was shown. If anyone later asks "where did the
Rs 9,50,000 figure come from?", the answer is traceable: STR_INPUT -> evidence_txn_ids
-> DT_TRANSACTIONS_ENRICHED -> RAW.RAW_AMLWORLD_TRANS -> the generated Parquet file.

### Task 3: The Adversarial Reviewer

**What:** A stored procedure called `REVIEW_FINDING(finding_id VARCHAR)`

**Why it exists:** An LLM-drafted narrative might contain errors: citing a transaction
that doesn't exist, naming a policy clause that was not in force on the transaction
date, or stating a number that doesn't match the database. The reviewer catches these
before the MLRO sees the draft.

**Where it fits:**

```
GENERATE_STR produces a FINDING row
    |
    v
REVIEW_FINDING checks it
    |
    +---> Deterministic checks (SQL, not LLM):
    |     - Every cited_evidence_id resolves to an EVIDENCE row?
    |     - Every cited_clause_id is in force on the txn date?
    |     - Every number in computed_values matches a re-run of the query?
    |
    +---> LLM check (DIFFERENT model family):
    |     - Is the narrative internally consistent?
    |     - Does it omit material evidence?
    |     - Is the conclusion supported by the cited evidence?
    |
    v
Writes a REVIEW row: APPROVE or REJECT with specific defects
```

**Why a different model family:** The STR narrative is drafted by an Anthropic model
(Claude Sonnet). The reviewer is deliberately from a different family (OpenAI GPT or
Meta Llama). This is defined in `conf/models.yml`:

```
drafter:   [claude-opus-5, claude-sonnet-5, claude-sonnet-4-6]   # Anthropic only
reviewer:  [openai-gpt-5.1, llama3.3-70b, llama3.1-70b]         # Never Anthropic
```

A critic that shares the writer's training data and blind spots is not a useful critic.
By using a different model family, we get genuinely independent verification.

### Task 4: The Attestation Router

**What:** A stored procedure called `REQUEST_ATTESTATION(finding_id VARCHAR)`

**Why it exists:** After the reviewer approves a finding, the draft needs to be packaged
as a formal artifact and routed to the MLRO for sign-off.

**How it works:**

1. Verifies the finding has a passing REVIEW (decision = 'APPROVE')
2. Assembles the STR content from FINDING + EVIDENCE
3. Computes a SHA-256 hash of the content
4. Creates an ARTIFACT row with status = 'DRAFT' and the content hash
5. Returns the artifact_id and instructions for the MLRO

**What it does NOT do:** It does not insert into ATTESTATION. That table can only be
written to by the `COMPLIANCE_OFFICER` role. The agent runs as `RC_SERVICE_ROLE`, which
does not have INSERT privilege on ATTESTATION. This is the structural human gate --
enforced by database privileges, not by a prompt instruction.

```
          CAN the agent do this?       WHO does it?
          -------------------------    ----------------
Step 1    Collect evidence             Agent (RC_SERVICE_ROLE)
Step 2    Draft STR narrative          Agent (RC_SERVICE_ROLE)
Step 3    Adversarial review           Agent (RC_SERVICE_ROLE)
Step 4    Create artifact              Agent (RC_SERVICE_ROLE)
Step 5    Sign off (attest)            HUMAN (COMPLIANCE_OFFICER role only)
Step 6    File with FIU-IND            HUMAN (external system)
```

### Task 5: The Cortex Search Service (Regulatory Knowledge)

**What:** A Cortex Search service called `REGULATORY_SEARCH_SVC` over chunked regulatory
text.

**Why it exists:** When the agent writes an STR, it needs to cite real regulations --
"PMLA Rule 3(1)(A)", "RBI Master Direction on KYC, 2016, Section 38". If the agent just
makes up citation numbers, the STR is legally worthless. The search service lets the
agent look up actual regulatory text.

**How it connects:**

```
data/SBA.pdf                          GOVERNANCE schema
data/FIU-IND_STR_Banking_Company.md   
    |                                 POLICY_CLAUSE_CHUNKS table
    | (AI_PARSE_DOCUMENT + chunking)  (chunked paragraphs with embeddings)
    v                                     |
POLICY_DOCS stage                         v
                                      REGULATORY_SEARCH_SVC
                                      (Cortex Search -- vector + keyword)
                                          |
                                          v
                                      Agent tool: "search_regulations"
                                      (returns relevant paragraphs)
```

The agent's tool specification includes this search service. When the orchestration model
decides it needs regulatory context, it queries the search service and gets back the
relevant paragraphs from actual PMLA rules and RBI circulars.

### Task 6: The Semantic View (Natural Language SQL)

**What:** A semantic view called `SV_RISK_COPILOT` over the CURATED and EVIDENCE_CHAIN
schemas.

**Why it exists:** Analysts sometimes need to ask data questions that aren't about a
specific case: "How many signals fired this week?", "Which branch has the most open
cases?", "Show me all HIGH severity signals." The semantic view lets the agent translate
natural language into SQL.

**How it connects:**

```
Analyst: "Show me all open cases in Delhi"
    |
    v
Agent's cortex_analyst tool
    |
    v
SV_RISK_COPILOT (semantic view)
    |
    | Translates to:
    | SELECT * FROM EVIDENCE_CHAIN.CASE_RECORD
    | WHERE status = 'OPEN' AND branch_region = 'DELHI'
    |
    v
Returns the result set to the agent, which formats it for the analyst
```

### Task 7: The CREATE AGENT Statement (Wiring It All Together)

**What:** The `CREATE AGENT` SQL statement that registers all the tools and instructions
with Snowflake.

**Why it exists:** This is the glue. Without it, the procedures exist but nobody can
use them through a conversational interface. The agent specification tells Snowflake:

- These are my tools (collect_case_evidence, generate_str, validate_str_inputs,
  review_finding, request_attestation)
- Each tool runs on this warehouse with this timeout
- Here are my instructions (system prompt)
- Here is my budget (120 seconds, 32K tokens per turn)

**How the tools orchestrate:**

```
User: "Generate STR for case CASE-4399c4c86db5c6ea"
    |
    | Orchestration model reads instructions + tool descriptions
    | Decides on a plan:
    |   1. Call collect_case_evidence(case_id)
    |   2. Call generate_str(input_json)
    |   3. Return the result
    |
    v
Tool call 1: collect_case_evidence('CASE-4399c4c86db5c6ea')
    |
    | Procedure runs on RC_DEMO_WH
    | Reads: CASE_RECORD, SIGNAL, DT_DIM_PARTY, DT_TRANSACTIONS_ENRICHED
    | Returns: { subject_name: "Rajesh Kumar", transactions: [...], ... }
    |
    v
Tool call 2: generate_str('{"subject_name":"Rajesh Kumar",...}')
    |
    | Procedure runs on RC_DEMO_WH
    | Validates input, inserts STR_INPUT, calls CORTEX.COMPLETE
    | Returns: { status: "SUCCESS", narrative: "## STR DRAFT\n..." }
    |
    v
Agent response: "Here is the STR draft for Rajesh Kumar..."
```

The orchestration model makes the tool-calling decisions. The agent specification gives
it the information it needs to make good decisions (tool descriptions, instructions).

---

## 9. How All the Pieces Orchestrate Together

Here is the complete flow from raw data to a signed STR, showing every component and
how they connect:

```
PHASE 1: DATA GENERATION (offline, on your laptop)
==========================================================

  AMLworld HI-Small (5M transactions, academic data)
       |
       | riskcopilot generate --profile dev
       | (15 validation gates, deterministic output)
       |
       v
  Parquet files (byte-identical per seed)
       |
       | riskcopilot load --profile dev
       | (one transaction, all or nothing)
       |
       v
  RAW schema (13 tables, 5,078,345 transaction rows)


PHASE 2: AUTOMATIC DETECTION (Snowflake runs this on its own)
==========================================================

  RAW tables
       |
       | Dynamic tables refresh automatically
       | (no cron, no Airflow, no scheduler)
       |
       v
  CURATED schema:
    DT_DIM_PARTY ................. party + KYC + accounts
    DT_TRANSACTIONS_ENRICHED ..... enriched transaction legs
    DT_TXN_DAILY_AGG ............ daily summaries
    DT_SIGNALS_FAST .............. 10 structuring signals
       |
       | SIGNALS_STREAM captures changes
       | ROUTE_SIGNALS_TASK fires within 30 seconds
       |
       v
  EVIDENCE_CHAIN schema:
    SIGNAL ......... 10 rows (deduplicated)
    CASE_RECORD .... 10 rows (one per party)
    CASE_SIGNAL .... 10 rows (signal-to-case links)


PHASE 3: AGENT-ASSISTED INVESTIGATION (when analyst asks)
==========================================================

  Analyst: "Generate STR for case CASE-xxx"
       |
       v
  RISK_COPILOT_AGENT (Cortex Agent)
       |
       | Tool 1: collect_case_evidence
       |   reads CASE_RECORD + SIGNAL + DT_DIM_PARTY + DT_TRANSACTIONS
       |   writes EVIDENCE rows (pointers, not copies)
       |   returns assembled JSON
       |
       | Tool 2: generate_str
       |   validates input
       |   calls CORTEX.COMPLETE (LLM writes the narrative)
       |   writes STR_INPUT + STR_OUTPUT + FINDING
       |   returns the draft
       |
       | Tool 3: review_finding (optional, analyst can request)
       |   deterministic checks (citations, numbers, dates)
       |   LLM check (different model family)
       |   writes REVIEW row
       |
       | Tool 4: request_attestation (after review passes)
       |   creates ARTIFACT with content_hash
       |   routes to MLRO
       |
       v
  STR DRAFT ready for human review


PHASE 4: HUMAN SIGN-OFF (never automated)
==========================================================

  MLRO (Compliance Officer role) reviews the STR draft
       |
       | Checks: narrative accuracy, regulatory citations,
       |         transaction details, policy alignment
       |
       +---> APPROVE: INSERT INTO ATTESTATION
       |     (content_hash binds the signature to this exact version)
       |     (ARTIFACT status -> APPROVED)
       |
       +---> RETURN: sends back to agent for revision
             (new FINDING version, supersedes_finding_id links to old one)
```

---

## 10. Cloud Architecture -- Why No External Hosting

### The traditional way (what you DON'T need to do):

```
                     THE HARD WAY
  Snowflake ----API----> Your server ----API----> OpenAI
  (data)                (Node.js/Python)          (LLM)
                        on Vercel/AWS/GCP
                        
  Problems:
  - Transaction data leaves Snowflake
  - You manage API keys for OpenAI
  - You manage hosting, scaling, monitoring
  - You manage auth between all three systems
  - Data transfer costs
  - Latency from network hops
```

### The Snowflake way (what we do):

```
                     THE SIMPLE WAY
  +-------------------------------------------+
  |            SNOWFLAKE ACCOUNT               |
  |                                            |
  |  Data (tables) ---> Agent (procedures)     |
  |                       |                    |
  |                       v                    |
  |               CORTEX.COMPLETE              |
  |               (built-in LLM)               |
  |                       |                    |
  |                       v                    |
  |               Response (JSON)              |
  |                                            |
  +-------------------------------------------+
         |
         | SQL API (built-in REST endpoint)
         v
  Any HTTP client (Postman, curl, React app, mobile app)
```

**Everything runs inside Snowflake:**
- Data never leaves the account
- LLM calls are a built-in function, not an external API
- The agent is a database object, not a running server
- The REST endpoint is Snowflake's SQL API, not something you deploy
- Auth is Snowflake auth (OAuth, key-pair, password -- whatever you already use)
- Scaling is automatic (warehouse auto-resume, auto-suspend)

**To "deploy to the cloud":** Run `riskcopilot deploy --profile dev`. That's it.
Snowflake IS the cloud.

**To get a REST endpoint:** Use Snowflake's SQL API:

```
POST https://<account>.snowflakecomputing.com/api/v2/statements
```

This endpoint already exists for every Snowflake account. You don't create it. You
don't deploy it. You just send SQL to it, and the SQL calls the agent.

---

## 11. Security and the Human Gate

### Role-Based Access Control

```
                    ACCOUNTADMIN
                         |
                    RC_ADMIN (owns all objects)
                    /         \
        RC_SERVICE_ROLE    COMPLIANCE_OFFICER
        (agent runs as)    (human reviewers)
              |                    |
              |              BRANCH_ANALYST_MUMBAI
              |              BRANCH_ANALYST_DELHI
              |              (filtered by row access policy)
              |
        Can do:              Cannot do:
        - Read all data      - Write ATTESTATION
        - Run agent          - Read GROUND_TRUTH
        - Write SIGNAL       - File with FIU-IND
        - Write CASE         - Change ARTIFACT to FILED
        - Write STR_INPUT
        - Write STR_OUTPUT
        - Write EVIDENCE
        - Write FINDING
        - Write REVIEW
        - Write ARTIFACT
```

### The Attestation Gate (Why the Agent Cannot File)

Under PMLA Rule 7, the Principal Officer must personally attest before filing. This is
enforced by database privileges:

```
ATTESTATION table
    |
    | INSERT privilege granted ONLY to COMPLIANCE_OFFICER
    | RC_SERVICE_ROLE (agent) does NOT have INSERT
    |
    | content_hash column: SHA-256 of the artifact content
    |   - If someone edits the STR after sign-off, the hash won't match
    |   - The filing procedure checks: artifact.content_hash = attestation.attested_content_hash
    |   - A mismatch blocks the filing
    |
    | No ARTIFACT can reach status FILED without a matching ATTESTATION row
```

This cannot be bypassed by prompt injection, jailbreaking, or any other AI attack.
It is a missing database privilege. The agent's role literally cannot execute the
INSERT statement.

### Ground Truth Isolation

The generated data includes labels: which parties are actually laundering. These labels
are in `GROUND_TRUTH.GROUND_TRUTH_STAGE`, and no role (not even COMPLIANCE_OFFICER)
can read them. This means:

- The agent cannot cheat by looking at the answer key
- Dynamic tables cannot use labels as detection features
- The detection system proves its value honestly

---

## 12. One-Command Deploy

### The deploy pipeline

The existing deployer (`src/risk_copilot/pipeline/deploy.py`) processes SQL stages
in a fixed order:

```
riskcopilot deploy --profile dev

Stage 0:  bootstrap        sql/00_bootstrap/       Database, schemas, warehouses, roles
Stage 1:  ddl              sql/01_ddl/             28+ tables across 7 schemas
Stage 2:  dynamic_tables   sql/02_dynamic_tables/  8 dynamic tables + 2 views
Stage 3:  streams_tasks    sql/03_streams_tasks/   Stream, router, triggered task
Stage 4:  governance       sql/04_governance/      Row access policies, masking
Stage 5:  policy           sql/05_policy/          Seeds POLICY_CLAUSE from YAML
Stage 6:  search           sql/06_search/          Cortex Search service
Stage 7:  semantic         sql/07_semantic/         Semantic View
Stage 8:  agent            sql/08_agent/           <<< THE AGENT LIVES HERE >>>
Stage 9:  ml               sql/09_ml/              ML model registration
```

Every SQL file is a Jinja template. The deployer substitutes values from
`conf/base.yml` + `conf/profiles/dev.yml`:

```
{{ fq.evidence }}  --->  RISK_COPILOT.EVIDENCE_CHAIN
{{ wh.demo }}      --->  RC_DEMO_WH
{{ cortex.agent }} --->  RISK_COPILOT_AGENT_DEV
```

This is what makes `--profile dev` and `--profile demo` coexist in one account: the
object names get different suffixes.

### What stage 8 (agent) deploys

```
sql/08_agent/
  01_str_tables.sql          STR_INPUT, STR_OUTPUT tables
  02_str_procedures.sql      VALIDATE_STR_INPUTS, GENERATE_STR
  03_evidence_collector.sql  COLLECT_CASE_EVIDENCE
  04_review.sql              REVIEW_FINDING
  05_attestation.sql         REQUEST_ATTESTATION
  06_agent.sql               CREATE AGENT with all tools
  07_grants.sql              Role grants for all objects
```

Each file uses `CREATE OR REPLACE` or `IF NOT EXISTS`, so running the deploy again
is safe -- it overwrites procedures and recreates the agent with any updated
instructions or tools.

### To deploy only the agent (after changes):

```bash
riskcopilot deploy --profile dev --stage agent
```

### To deploy everything from scratch:

```bash
riskcopilot deploy --profile dev
```

---

## 13. Talking to the Agent

Once deployed, there are four ways to interact with the agent. All of them ultimately
call the same function: `SNOWFLAKE.CORTEX.DATA_AGENT_RUN`.

### Way 1: Snowsight (Browser UI)

Open Snowsight > AI & ML > Agents > RISK_COPILOT_AGENT_DEV. Type in the chat box.

### Way 2: SQL (Any SQL Client)

```sql
SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(
    'RISK_COPILOT.EVIDENCE_CHAIN.RISK_COPILOT_AGENT_DEV',
    '{"messages":[{"role":"user","content":[{"type":"text",
      "text":"Generate STR for case CASE-4399c4c86db5c6ea"}]}]}',
    TRUE   -- TRUE = streaming response
) AS response;
```

### Way 3: REST API (Any HTTP Client)

```
POST https://HQRWRXK-UZ26728.snowflakecomputing.com/api/v2/statements

Headers:
  Authorization: Bearer <your-token>
  Content-Type: application/json

Body:
{
  "statement": "SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(:1, :2, TRUE)",
  "bindings": {
    "1": {"type":"TEXT", "value":"RISK_COPILOT.EVIDENCE_CHAIN.RISK_COPILOT_AGENT_DEV"},
    "2": {"type":"TEXT", "value":"{\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Generate STR for case CASE-xxx\"}]}]}"}
  },
  "warehouse": "RC_DEMO_WH",
  "database": "RISK_COPILOT",
  "schema": "EVIDENCE_CHAIN"
}
```

This is the "cloud endpoint". Any application -- a React frontend, a mobile app,
a Slack bot, a Python script -- can POST to this URL.

### Way 4: Python

```python
import snowflake.connector, json

conn = snowflake.connector.connect(
    account="HQRWRXK-UZ26728",
    user="RAGHAV2310",
    # ... your auth
)

messages = json.dumps({"messages": [
    {"role": "user", "content": [
        {"type": "text", "text": "Generate STR for case CASE-xxx"}
    ]}
]})

result = conn.cursor().execute(
    "SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s, TRUE)",
    ("RISK_COPILOT.EVIDENCE_CHAIN.RISK_COPILOT_AGENT_DEV", messages)
).fetchone()[0]

print(json.loads(result))
```

---

## 14. Design Decision Log

| ID | Decision | Why | Alternative Rejected |
|----|----------|-----|---------------------|
| D1 | Everything runs inside Snowflake, no external hosting | Data never leaves; no infra to manage | Web app on Vercel/AWS -- adds complexity, security risk, cost |
| D2 | Thresholds live in POLICY_CLAUSE, not in code | Changing a threshold is a governed data change with audit trail, not a code deploy | Hardcoded values in SQL -- no audit trail, requires code review for a number change |
| D3 | Agent runs as RC_SERVICE_ROLE, not ACCOUNTADMIN | Principle of least privilege; cannot access GROUND_TRUTH or ATTESTATION | Running as admin -- too many privileges, no separation of duties |
| D4 | ATTESTATION requires COMPLIANCE_OFFICER role | PMLA Rule 7 legal requirement; structural enforcement, not prompt-based | Prompt instruction "don't file automatically" -- can be bypassed |
| D5 | Reviewer uses a different AI model family | Independent verification; different training data and blind spots | Same model reviews its own work -- confirmation bias |
| D6 | Evidence rows are pointers, not copies | Copied data diverges from the source over time; a pointer + as_of_ts stays true | Copy-on-read -- eventually inconsistent, harder to re-verify |
| D7 | Signal deduplication via MERGE on business_key | DTs recompute fully on every refresh; without MERGE, duplicates grow linearly | Delete-and-reinsert -- loses first_seen_at history, not atomic |
| D8 | Stream-triggered task, not scheduled polling | Runs within 30s of a real change; never wastes compute on empty polls | CRON schedule -- either too frequent (wasteful) or too slow |
| D9 | Numbers in STR come from SQL, narrative from LLM | "No number in a filing comes from a language model" -- the FINDING.computed_values mechanism | Let the LLM compute amounts -- unreliable, not auditable |
| D10 | Content hash binds attestation to exact artifact version | Post-attestation edits are caught mechanically | Trust-based: "nobody would edit after sign-off" -- unenforceable |
| D11 | Cortex Search for regulatory citations | Agent cites real PMLA sections, not hallucinated circular numbers | LLM generates citations from training data -- often wrong |
| D12 | One-command deploy via riskcopilot CLI | Reproducible, idempotent, profile-aware; no manual SQL execution | Run SQL files by hand in Snowsight -- error-prone, environment-specific |
