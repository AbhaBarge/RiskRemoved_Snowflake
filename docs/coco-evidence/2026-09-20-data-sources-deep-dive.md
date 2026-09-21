# Data Sources Deep Dive — Risk & Compliance Copilot

**Session date:** 2026-09-20
**Purpose:** Complete data source architecture — ingestion, storage, transformation, signal detection, and notification. Grounded in verified Snowflake capabilities and confirmed Marketplace listings. No guesswork.

---

## Design Principle: Complement, Do Not Compete

The copilot does not replace the Core Banking System, the Loan Management System, or the Transaction Monitoring System. Those systems continue to do what they do well — process transactions, manage loans, fire rule-based alerts. The copilot sits downstream, consuming their outputs through well-defined extraction points, and does what they cannot: join across silos, maintain an evidence chain, structure policy text, and produce audit-ready artifacts.

**The CBS is the system of record. The TMS is the detection engine. The copilot is the investigation, reasoning, and reporting layer that bridges them.**

---

## Data Source Architecture — Four Categories

### Category 1: Transactional Data from CBS

**Extraction schema (conformed, not raw CBS):**

| Field | Why |
|---|---|
| txn_id | Unique identifier, primary key |
| account_id | Debit account |
| counterparty_account_id | Credit account or external identifier |
| txn_timestamp | Exact posting time |
| amount | In original currency |
| currency | ISO 4217 |
| txn_type | CREDIT, DEBIT, TRANSFER, CASH_DEPOSIT, CASH_WITHDRAWAL, CARD_PAYMENT |
| channel | BRANCH, ATM, MOBILE, INTERNET, POS, SWIFT, NEFT, RTGS, UPI, IMPS |
| merchant_category_code | For card transactions |
| geo_lat, geo_lon | For geolocated channels |
| device_fingerprint | For digital channels |
| ip_address | For internet/mobile channels |
| reference_text | Free text narration |

**Ingestion: Snowpipe Streaming (High-Performance Architecture)**
- Up to 20 GB/s throughput per table
- As low as 5 seconds ingest-to-queryable latency
- Elastic Channels for at-least-once delivery (deduplicate downstream)
- Named Channels with offset tokens for exactly-once (Kafka sources)
- In-flight transformations via COPY command syntax in PIPE object
- Schema evolution — new columns added without manual DDL

**Storage:** Landing table RAW_TRANSACTIONS, append-only. Immutable audit spine. Time Travel for point-in-time evidence reproduction.

**Transformation — Fast Lane (Dynamic Tables):**

```
RAW_TRANSACTIONS (landing, Snowpipe Streaming)
    |
    v
DT_TRANSACTIONS_ENRICHED (TARGET_LAG = '1 minute')
    - Joins to DIM_ACCOUNT, DIM_PARTY for customer context
    - Computes: is_round_amount, is_just_below_threshold flags
    - Tags channel type and geo region
    |
    v
DT_TXN_VELOCITY_FEATURES (TARGET_LAG = '5 minutes')
    - Per-account rolling features:
      - 1h/24h/7d/30d transaction count and sum
      - Distinct counterparty count per window
      - Channel entropy
      - Time-of-day distribution deviation
      - Max single txn as % of 30d average
    |
    v
DT_SIGNALS_FAST (TARGET_LAG = '5 minutes')
    - Detection rules over velocity features
    - Output: signal_id, party_id, account_id, rule_id,
              fired_at, severity, evidence_txn_ids, as_of_ts
```

All dynamic tables use REFRESH_MODE = INCREMENTAL (supported for joins, aggregations, window functions per Snowflake docs).

---

### Category 2: Entity and Relationship Data (CBS + KYC Platform)

**2a. Party (Customer Master)**

| Field | Source | Why |
|---|---|---|
| party_id | CBS customer ID | Entity resolution anchor |
| party_type | CBS | INDIVIDUAL, CORPORATE, TRUST, HUF |
| full_name | CBS | Display and search |
| date_of_birth / incorporation_date | CBS | Age-based risk, sanctions matching |
| nationality / jurisdiction | KYC | FATF jurisdiction risk |
| occupation / business_type | KYC | Expected transaction profile |
| declared_annual_income | KYC | Income-vs-activity mismatch detection |
| risk_rating | KYC | HIGH / MEDIUM / LOW |
| pep_flag | KYC / PEP screening | AML risk scoring |
| kyc_status | KYC | VERIFIED / PENDING / EXPIRED |
| source_of_funds | KYC | Comparison against actual flows |
| beneficial_owner_party_ids | KYC | Ownership chain for UBO analysis |

**2b. PartyRelationship — Graph Spine**

Constructed from multiple sources during ingestion:

| Relationship type | Source |
|---|---|
| OWNS_ACCOUNT | CBS |
| BENEFICIAL_OWNER_OF | KYC (UBO chain) |
| DIRECTOR_OF | KYC / MCA filings |
| SHARED_PHONE | KYC onboarding data |
| SHARED_ADDRESS | KYC onboarding data |
| SHARED_DEVICE | Digital banking platform |
| SHARED_IP | Digital banking platform |
| FUND_FLOW | Derived from transactions (aggregated) |
| GUARANTOR_OF | LMS |
| CO_BORROWER | LMS |

**2c. Account Master**

| Field | Source |
|---|---|
| account_id | CBS |
| party_id | CBS (FK to DIM_PARTY) |
| account_type | SAVINGS, CURRENT, LOAN, CREDIT_CARD, FIXED_DEPOSIT |
| opened_date | CBS |
| status | ACTIVE, DORMANT, FROZEN, CLOSED |
| current_balance | CBS (daily snapshot) |
| credit_limit | CBS (credit accounts) |

**Ingestion:** Daily batch COPY INTO from CBS/KYC exports (CSV/JSON to stage).

**Transformation:**

```
RAW_PARTY_CBS + RAW_PARTY_KYC
    |
    v
DT_DIM_PARTY (TARGET_LAG = '1 day')
    - Entity resolution: match CBS customer to KYC record
    - Resolve duplicates (same PAN, same phone + DOB)
    - Output: one conformed row per party with match_confidence

DT_PARTY_RELATIONSHIP (TARGET_LAG = '1 day')
    - Union all relationship types
    - FUND_FLOW edges derived from DT_TRANSACTIONS_ENRICHED
```

Balance snapshots stored as SCD Type 2 (daily insert with as_of_date) to support point-in-time balance lookups for evidence reproduction.

---

### Category 3: External Reference Data

#### 3a. Sanctions and Watchlists

**Public sources (free, machine-readable):**

| Source | Format | Refresh |
|---|---|---|
| UN Security Council Consolidated List | XML | On change |
| OFAC SDN + Consolidated Lists (US Treasury) | XML, CSV, JSON | Multiple times/week |
| EU Consolidated Sanctions List | XML | On change |
| UK OFSI Consolidated List | XML, CSV, ODS | On change |
| MHA UAPA Designated Entities (India) | PDF | Irregular |

**Ingestion for XML/CSV sources:**

```
SCHEDULED TASK (daily)
    |
    v
1. External Access Integration -> HTTPS GET to source URL
2. Response written to internal stage @SANCTIONS_RAW
3. COPY INTO RAW_SANCTIONS_UN / RAW_SANCTIONS_OFAC / etc.
   - XML parsed using XMLGET / LATERAL FLATTEN
   - CSV parsed directly
4. DT_SANCTIONS_CONSOLIDATED (dynamic table, TARGET_LAG = DOWNSTREAM)
   - Union all lists
   - Normalize: entity_name, aliases[], id_documents[],
     nationality, date_of_birth, list_source, listed_date
   - Deduplicate across lists
```

For MHA UAPA (PDF only): AI_PARSE_DOCUMENT in LAYOUT mode -> AI_EXTRACT for structured fields.

**Snowflake Marketplace listings (confirmed available):**

| Listing | Provider | What |
|---|---|---|
| FactSet Global Sanctions (GZT0Z28ANXQ) | FactSet | US, EU, UN, UK and global sanctions lists |
| Morningstar Sustainalytics Country Screening (GZU6Z6T0HW0) | Morningstar | 172 countries screened against UN, US, EU sanctions |
| Politically Exposed Persons Dataset (GZSYZMQ0R3) | TRUVITY | PEPs and Related/Close Associates |
| Adverse Media Entities Data Set (GZSYZMQ0QY) | TRUVITY | AML/CTF adverse media on entities |
| Company and Supply Chain Risk Data (GZT0ZR4D3QN) | Exiger | AI-powered third-party and supply chain risk |
| OpenCorporates Relationship Data (GZTDZM1AKD) | OpenCorporates | Company connections and ownership across 140+ jurisdictions |

**Decision:** Ingest real UN and OFAC lists directly (free, public, strong credibility). Supplement with TRUVITY PEP dataset from Marketplace (demonstrates Marketplace integration).

#### 3b. FATF High-Risk and Monitored Jurisdictions

Published ~3x/year at plenaries. Small dataset (20-30 countries), changes rarely.

```
REF_FATF_JURISDICTIONS (manually maintained reference table)
    - jurisdiction_code (ISO 3166-1 alpha-2)
    - list_type: HIGH_RISK | INCREASED_MONITORING
    - listed_since, last_plenary_reviewed
    - notes

Updated via scheduled automation:
1. Fetch FATF page
2. AI_EXTRACT to pull country list
3. Diff against current table
4. PROPOSED change to review queue
5. Human approves -> activated
```

#### 3c. IP Intelligence

**Marketplace listings (confirmed):**

| Listing | Provider | What |
|---|---|---|
| IP Intelligence for Fraud Prevention (GZSTZSHKQ5EL) | IPinfo Inc. | VPN/proxy/Tor detection, IP-to-geo, risk scoring |
| Fraud Blocker IP Database (GZ2FQZ1KTG4Q) | Fraud Blocker | Tor, VPN, fraud score, datacenter detection |
| MaxMind Compliance Solution (GZ2FTZ16WXQQF) | MaxMind | Anonymizer detection, geo-confidence, compliance-grade IP |

**Ingestion:** Marketplace zero-copy share. No ETL. Join directly in dynamic tables.

#### 3d. Credit Risk Reference Data

**Marketplace listings (confirmed):**

| Listing | Provider | What |
|---|---|---|
| Global Probability of Default Suite (GZU6ZOVBL5) | Criat | Forward-looking, market-implied PD estimates |
| Credit Benchmark Consensus Ratings (GZSVZPHWVR) | Credit Benchmark | Creditworthiness on 64K+ counterparties from 40+ FIs |
| Fitch XGS Credit Ratings (GZU6Z5C46JC4R) | Fitch Solutions | Basel III-ready credit ratings for banks and NBFIs |
| IBISWorld Industry Data (GZSTZLT2IJR) | IBISWorld | Industry data for credit risk, portfolio analytics, benchmarks |

#### 3e. Adverse Media

TRUVITY Adverse Media (GZSYZMQ0QY) confirmed on Marketplace. For production, RavenPack News Analytics (GZTSZ9DN1IO) provides structured sentiment from 40,000+ news sources (commercial).

---

### Category 4: Regulatory and Policy Text

**4a. RBI Circulars and Master Directions**

Extraction schema for POLICY_CLAUSE:

| Field | Example |
|---|---|
| clause_id | Auto-generated, stable across versions |
| source_document | "Master Direction - Monitoring of Frauds in NBFCs (Ref. No. RBI/2024-25/XX)" |
| source_page | 12 |
| section_heading | "Classification of Frauds" |
| clause_text | Full clause text |
| effective_from | 2024-04-01 |
| effective_to | NULL (current) |
| applicability | NBFC-ML, NBFC-UL |
| metric | fraud_amount |
| operator | BETWEEN |
| threshold_lower | 100000 |
| threshold_upper | 2500000 |
| action_required | "Report to RBI within 21 days" |
| supersedes_clause_id | Previous version's clause_id |

**Ingestion pipeline:**

```
INTERNAL STAGE @POLICY_DOCS
    | (PDF uploaded or scheduled download)
    v
TASK: PARSE_NEW_CIRCULARS
    |
    |- AI_PARSE_DOCUMENT(TO_FILE('@POLICY_DOCS', filename),
    |      {'mode': 'LAYOUT', 'page_split': true})
    |  -> Markdown with tables and headers preserved
    |
    |- AI_EXTRACT(parsed_text,
    |      'Extract all regulatory rules, thresholds, timelines...')
    |  -> Structured JSON array of clause objects
    v
RAW_POLICY_CLAUSES (staging)
    v
TASK: DIFF_AND_PROPOSE
    - Compare to current POLICY_CLAUSE
    - Identify NEW, CHANGED, SUPERSEDED
    - Write to POLICY_CHANGE_PROPOSALS (review queue)
    - DO NOT activate
    v
[HUMAN REVIEW]
    - Approves or rejects each proposal
    - On approval: INSERT into POLICY_CLAUSE with effective_from
    v
POLICY_CLAUSE (governed, versioned rule table)
```

**4b. FIU-IND Reporting Templates**

```
REF_FILING_TEMPLATES
    - template_id
    - report_type: STR | CTR | CCR | NTR | CBWTR
    - version
    - effective_from
    - field_definitions: VARIANT (JSON array of field specs)
    - narrative_structure: VARIANT (for STR: required sections)
```

---

### Category 5: Credit Exposure Data from LMS

| Field | Why |
|---|---|
| loan_account_id | Primary key |
| party_id | FK to DIM_PARTY (cross-silo join key) |
| product_type | TERM_LOAN, WORKING_CAPITAL, OVERDRAFT, GOLD_LOAN |
| sanctioned_amount | Original loan amount |
| outstanding_principal | Current balance |
| dpd | Days past due |
| collateral_type | PROPERTY, GOLD, SECURITIES, UNSECURED |
| collateral_value | Last valuation |
| restructured_flag | Whether restructured |
| npa_date | Date classified as NPA |

**Ingestion:** Daily batch COPY INTO from LMS export.

**Transformation — Slow Lane:**

```
RAW_EXPOSURES (daily batch)
    |
    v
DT_EXPOSURES_STAGED (TARGET_LAG = '1 day')
    - Compute DPD, classify SMA-0/1/2, NPA
    - Sub-classify NPA: SUBSTANDARD/DOUBTFUL/LOSS
    - Thresholds from POLICY_CLAUSE table
    - Join to DT_DIM_PARTY
    - Flag: has_open_fraud_case, has_aml_alert (from CASE table)
    |
    v
DT_EXPOSURE_PORTFOLIO (TARGET_LAG = '1 day')
    - Total outstanding by stage
    - Migration matrix
    - Sector concentration
    - Large exposure flags
    |
    v
DT_PROVISIONING (TARGET_LAG = '1 day')
    - ECL: Stage 1 (12m PD x LGD x EAD),
           Stage 2 (Lifetime PD x LGD x EAD),
           Stage 3 (Lifetime PD x LGD x (EAD - collateral))
    - PD/LGD from POLICY_CLAUSE
```

**The cross-silo bridge:** DT_EXPOSURES_STAGED joins to CASE table to check whether borrower has open fraud/AML cases. This is the join no existing system makes.

---

### Category 6: Balance Sheet Data for Liquidity (If Scope Permits)

| Field | Why |
|---|---|
| gl_code | General ledger account code |
| balance | As-of-date balance |
| maturity_date | For bucketing |
| lcr_classification | HQLA_L1/L2A/L2B, OUTFLOW_RETAIL/WHOLESALE, INFLOW |
| haircut_rate | From POLICY_CLAUSE |
| runoff_rate | From POLICY_CLAUSE |

**Transformation:**

```
DT_LCR_COMPUTATION (TARGET_LAG = '1 day')
    - HQLA = SUM(balance * (1 - haircut_rate))
    - Outflows = SUM(balance * runoff_rate)
    - Inflows = SUM(balance * inflow_rate), capped at 75% of outflows
    - LCR = HQLA / (Outflows - Inflows)
    - Haircuts/runoff rates joined from POLICY_CLAUSE (effective-dated)
```

---

## Signal Detection — Cross-Domain (What the TMS Cannot Do)

| Signal | What It Detects | Data Joined | Why TMS Cannot |
|---|---|---|---|
| INCOME_ACTIVITY_MISMATCH | Inflows in 30d > Nx declared annual income | Transactions + KYC | TMS lacks declared income |
| SHARED_ATTRIBUTE_CLUSTER | 3+ accounts sharing phone/device/IP with fund flows | PartyRelationship graph | TMS has no graph model |
| MULE_RING_CANDIDATE | Depth-3 fund-flow cycle through shared-attribute nodes | Recursive CTE over PartyRelationship | TMS cannot do graph traversal |
| FRAUD_BORROWER_OVERLAP | Borrower with exposure AND fraud/AML signals | Exposures + Cases | TMS has no credit data |
| SANCTIONS_MATCH_NEW | Customer matches newly added sanctions entry | DIM_PARTY + SANCTIONS_CONSOLIDATED | Copilot adds full context |
| PEP_TRANSACTION_ANOMALY | PEP with transactions inconsistent with declared SOF | Transactions + KYC + PEP list | TMS cannot compare to SOF |
| STRUCTURING_CROSS_ACCOUNT | Sub-threshold txns split across accounts of same party | Transactions via DIM_PARTY | TMS is account-level, not party-level |

---

## Notification — Signal to Action

```
STREAM: SIGNALS_STREAM ON DYNAMIC TABLE DT_SIGNALS_FAST

TASK: ROUTE_SIGNALS
    WHEN SYSTEM$STREAM_HAS_DATA('SIGNALS_STREAM')
AS
    - Check if open case exists for this party
    - If yes: attach signal to existing case
    - If no and severity >= threshold: create new case
    - Route notification:
      - FRAUD signals -> Slack #fraud-ops (MCP)
      - AML signals -> compliance queue
      - CROSS_DOMAIN -> both + CRO dashboard
    - Write to CASE and SIGNAL tables
    - SANCTIONS_MATCH: immediate high-priority alert
```

Hybrid pattern: dynamic table (declarative) -> stream (capture changes) -> task (procedural routing). Task fires only when stream has unconsumed changes.

---

## Complete Storage Model

| Table | Type | Purpose | Mutability |
|---|---|---|---|
| RAW_TRANSACTIONS | Regular | Landing for Snowpipe Streaming | Append-only |
| RAW_EXPOSURES | Regular | Landing for daily LMS batch | Append-only (daily snapshots) |
| RAW_SANCTIONS_* | Regular | Landing for each sanctions source | Replace on refresh |
| DT_TRANSACTIONS_ENRICHED | Dynamic | Joined txns with account/party context | Managed |
| DT_TXN_VELOCITY_FEATURES | Dynamic | Rolling window features per account | Managed |
| DT_SIGNALS_FAST | Dynamic | Fraud/AML detection rules output | Managed |
| DT_DIM_PARTY | Dynamic | Conformed party dimension | Managed |
| DT_PARTY_RELATIONSHIP | Dynamic | Graph edges for network analysis | Managed |
| DT_SANCTIONS_CONSOLIDATED | Dynamic | Unified sanctions list | Managed |
| DT_EXPOSURES_STAGED | Dynamic | Credit staging with cross-silo flags | Managed |
| DT_PROVISIONING | Dynamic | ECL computation | Managed |
| DT_LCR_COMPUTATION | Dynamic | Liquidity coverage ratio | Managed |
| POLICY_CLAUSE | Regular | Effective-dated regulatory rules | Insert + soft-supersede |
| SIGNAL | Regular | Persisted signal records | Append-only |
| CASE | Regular | Investigation cases | Status transitions only |
| EVIDENCE | Regular | Pointers to source data | Append-only |
| FINDING | Regular | Reasoned conclusions citing evidence + policy | Versioned |
| ARTIFACT | Regular | Generated outputs (STR, reports) | Versioned |
| ATTESTATION | Regular | Human sign-offs | Append-only, immutable |
| REF_FATF_JURISDICTIONS | Regular | FATF high-risk/monitored countries | Manual + review queue |
| REF_FILING_TEMPLATES | Regular | FIU-IND reporting formats | Versioned |

---

## Data Flow Diagram

```
                    EXTERNAL SOURCES
    +-------------+  +----------+  +-----------+  +----------+
    | CBS         |  | KYC/CDD  |  | LMS       |  | GL       |
    | Transactions|  | Platform |  | Exposures |  | Balances |
    +------+------+  +----+-----+  +-----+-----+  +----+-----+
           |              |              |              |
    Snowpipe        Daily batch    Daily batch    Monthly batch
    Streaming       COPY INTO      COPY INTO      COPY INTO
           |              |              |              |
    +------v------+  +----v-----+  +-----v-----+  +----v-----+
    | RAW_        |  | RAW_     |  | RAW_      |  | RAW_     |
    | TRANSACTIONS|  | PARTY_*  |  | EXPOSURES |  | GL_POS   |
    +------+------+  +----+-----+  +-----+-----+  +----+-----+
           |              |              |              |
    =======|==============|==============|==============|=======
           |      DYNAMIC TABLE PIPELINE (Snowflake-managed)
           |              |              |              |
    +------v------+  +----v-----+  +-----v-----+  +----v-----+
    | DT_TXN_    |  | DT_DIM_  |  | DT_EXPO-  |  | DT_LCR_  |
    | ENRICHED   |<-+ PARTY    +-->  SURES_    |  | COMPUTA- |
    | (1 min)    |  | (1 day)  |  | STAGED    |  | TION     |
    +------+------+  +----+-----+  | (1 day)   |  | (1 day)  |
           |              |        +-----+-----+  +----------+
    +------v------+  +----v-----+        |
    | DT_TXN_    |  | DT_PARTY |        |
    | VELOCITY   |  | RELATION-|    Cross-silo
    | FEATURES   |  | SHIP     |    join on
    | (5 min)    |  | (1 day)  |    party_id
    +------+------+  +----+-----+        |
           |              |              |
    +------v--------------v--------------v---------+
    | DT_SIGNALS_FAST (5 min)                      |
    | - Velocity rules                             |
    | - Graph traversal (recursive CTE)            |
    | - Cross-domain joins                         |
    | - Sanctions screening                        |
    | - Policy clause threshold checks             |
    +------------------+---------------------------+
                       |
    ===================|============================
                       | STREAM + TASK (side effects)
    +------------------v---------------------------+
    | SIGNALS_STREAM -> ROUTE_SIGNALS task         |
    | - Create/update CASE                         |
    | - Attach EVIDENCE (pointers, not copies)     |
    | - Notify via Slack/email (MCP)               |
    | - Route to investigation queue               |
    +------------------+---------------------------+
                       |
    +------------------v---------------------------+
    | AGENT CHAIN (on demand or by case SLA clock) |
    | - Gather evidence bundle                     |
    | - Map to policy clauses                      |
    | - Draft finding + artifact (STR, report)     |
    | - Numeric reconciliation gate                |
    | - Adversarial reviewer                       |
    | - Queue for human attestation                |
    +----------------------------------------------+

    EXTERNAL REFERENCE DATA (parallel ingestion)
    +----------+  +----------+  +----------+  +----------+
    | UN/OFAC/ |  | TRUVITY  |  | IPinfo/  |  | RBI      |
    | EU/UK    |  | PEP list |  | MaxMind  |  | Circulars|
    | Sanctions|  | (Mktplc) |  | (Mktplc) |  | (PDFs)   |
    +----+-----+  +----+-----+  +----+-----+  +----+-----+
    Daily task    Zero-copy     Zero-copy     AI_PARSE_DOC
    HTTP GET ->   share ->      share ->      AI_EXTRACT ->
    stage ->      direct join   direct join   review queue
    COPY INTO
```

---

## Explicit Boundaries — What the Copilot Does NOT Do

1. Does not replace the CBS. Does not process or post transactions.
2. Does not replace the TMS. Does not make real-time block/allow decisions.
3. Does not replace the LMS. Does not manage loans or compute interest.
4. Does not replace KYC operations. Does not onboard customers.
5. Does not auto-activate regulatory changes. Human approval required.
6. Does not file STRs autonomously. Generates drafts; human attests and files.
7. Does not ingest real customer data. Entire dataset is synthetic.

---

## Confirmed Marketplace Listings Summary

| Provider | Listing | Domain | Type |
|---|---|---|---|
| FactSet | Global Sanctions (GZT0Z28ANXQ) | Sanctions | Data |
| Morningstar | Country Screening (GZU6Z6T0HW0) | Sanctions/Norms | Data |
| TRUVITY | PEPs Dataset (GZSYZMQ0R3) | PEP Screening | Data |
| TRUVITY | Adverse Media (GZSYZMQ0QY) | AML/CTF | Data |
| IPinfo | IP Intelligence (GZSTZSHKQ5EL) | Fraud IP | Data |
| Fraud Blocker | Fraudulent IP DB (GZ2FQZ1KTG4Q) | Fraud IP | Data |
| MaxMind | Compliance Solution (GZ2FTZ16WXQQF) | Compliance IP | Data |
| Boolean Data | AML & Fraud Command Center (GZU6Z3U9MOEO7) | AML/Fraud | Native App |
| OpenCorporates | Relationship Data (GZTDZM1AKD) | Entity/Ownership | Data |
| Criat | Global PD Suite (GZU6ZOVBL5) | Credit Risk | Data |
| Credit Benchmark | Consensus Ratings (GZSVZPHWVR) | Credit Risk | Data |
| Fitch | XGS Credit Ratings (GZU6Z5C46JC4R) | Credit Risk | Data |
| Exiger | Supply Chain Risk (GZT0ZR4D3QN) | Third-party Risk | Data |
