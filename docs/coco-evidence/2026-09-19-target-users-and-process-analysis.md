# Target Users & Process Analysis — Risk & Compliance Copilot

**Session date:** 2026-09-19
**Purpose:** Deep analysis of target users, their current workflows, the data sources they use, and the exact processes the copilot replaces — mapped to the PS requirements.

---

## PS Requirements Decomposed

| # | PS text | What it actually requires |
|---|---|---|
| **R1** | "Combine transaction and account data with policy and filing text" | A joined data layer: structured (transactions, accounts, exposures) + unstructured (policy docs, circulars, filing templates) |
| **R2** | "Let a business or compliance user ask questions" | Natural language interface for non-technical users |
| **R3** | "Get governed, explainable, evidence-backed answers" | Every answer cites its source data, the rule that applies, and the query that produced the number |
| **R4** | "Cover the flow from signal to evidence to a documented finding or report" | A persisted pipeline: detection → investigation → evidence gathering → reasoning → output artifact |
| **R5** | "Produces audit-ready regulatory outputs" | The final deliverable is a document a regulator would accept — not a dashboard, not a chat answer |

---

## Target User 1: The AML Compliance Officer

**Who they are:** Mid-career compliance professional, 5-15 years experience. Reports to the MLRO (Money Laundering Reporting Officer) or Chief Compliance Officer. Not technical — they use the systems they are given but do not write queries or code. They are legally accountable for the quality of what they file.

### Process A: Transaction monitoring alert triage

**Current workflow:**

1. Log into the TMS (Actimize, SAS, Mantas, or similar). See a queue of 40-80 alerts assigned for the day.
2. Open alert #1. The TMS shows: "Account 4521XXXX received 7 inward transfers totalling ₹6.8L from 5 distinct remitters in 48 hours. Rule: RAPID_CREDIT_VELOCITY."
3. The TMS shows the transactions that fired the rule. Nothing else.
4. To investigate, the officer opens a SECOND application — the core banking system (Finacle, T24, Flexcube). Searches for the account. Pulls the full transaction history for the last 90 days. Reads it on screen or exports to Excel.
5. Opens a THIRD application — the KYC/CDD platform. Pulls the customer profile: occupation (salaried, ₹4.2L annual income), risk rating (medium), KYC renewal date, source of funds declared at onboarding.
6. Mentally compares: ₹6.8L inward in 48 hours vs ₹4.2L declared annual income. Notes the mismatch.
7. Wants to know: who are the 5 remitters? Goes back to CBS. Searches each remitter account one by one. 3 are customers of the same bank. 2 are external (only account numbers visible, no profile).
8. For the 3 internal remitters, pulls their profiles from the KYC platform. Reads each one. Notices two list the same employer. Notices one shares a mobile number with the original account holder. This is not flagged by any system — the officer spotted it by reading three separate screens.
9. Decision point: is this suspicious enough to escalate, or close with a note? The officer writes a 2-3 line disposition in the case management tool and either closes or escalates to the next level.
10. Time spent on this single alert: 25-45 minutes. Of which actual analysis/judgement: ~5 minutes. Of which data gathering across systems: ~20-40 minutes.

**What the copilot replaces:**

- Steps 4-8 are **R1** — the copilot joins transaction data, account data, and customer profile in one semantic layer. The officer asks "show me the full picture for this alert" and gets: transaction timeline, customer profile, counterparty profiles, and any shared attributes (phone, address, device, employer) — in one response, with every data point citing its source table and as-of timestamp (**R3**).
- Step 6 is **R4** — the income-vs-inflow mismatch is a signal that should be detected automatically by a rule over the joined data, not by an officer doing mental arithmetic.
- Step 8 is **R4** — the shared-mobile-number link is a signal that a graph query over `PartyRelationship` surfaces in seconds, not something that depends on an officer happening to read three screens carefully on a busy day.
- Step 9 remains human. The copilot does not decide. It presents the signals, the evidence, and the applicable policy clause, and the officer decides.

### Process B: STR drafting and filing

This is the highest-value process the copilot targets. An STR (Suspicious Transaction Report) is filed with FIU-IND under PMLA. It is a narrative document — not a form with checkboxes.

**Current workflow:**

1. The investigation (Process A, escalated) concludes that filing is warranted. The investigator or compliance officer opens a Word template.
2. Writes the narrative. Required structure: Subject identification (name, accounts, ID numbers); Description of suspicious activity (what happened, when, how much, what pattern); Why it is suspicious (which rule/threshold/behavioural indicator); What the institution has done (account restrictions, enhanced monitoring); Supporting evidence summary.
3. Every number in this narrative is manually transcribed from the Excel extracts and screen captures gathered during investigation. There is no automated link between the narrative and the source data.
4. The draft goes to the MLRO for review. The MLRO reads it, checks whether the narrative is consistent, whether the amounts add up, whether the cited rule is the right one. Often sends back for revision — "the total you cite in paragraph 3 doesn't match the transaction list in the appendix" or "you cited the old threshold, it changed last quarter."
5. 1-3 revision cycles over 2-5 working days.
6. MLRO signs off. Filed via the FIU-IND portal.
7. The filed STR is saved as a PDF somewhere — a shared drive, a document management system, or the case management tool. The link between the filed document and the underlying data is: the officer's memory and the Excel files they saved.

**What the copilot replaces:**

- Step 2 is **R5** — the copilot drafts the narrative from the evidence bundle. Every sentence cites a `(query_id, row_ids, as_of_timestamp)` triple. The officer reviews and edits the prose, but does not transcribe numbers.
- Step 3 is eliminated. Numbers are not transcribed — they are pulled from query results and embedded with provenance. The numeric reconciliation gate (**R3**) programmatically verifies every numeral in the narrative traces to a result set before the officer sees it.
- Step 4 is accelerated. The MLRO can click any number and see the query that produced it, re-executed at the same point in time via Time Travel. The "does paragraph 3 match the appendix" problem disappears — they are the same data.
- Step 7 is replaced by a persisted artifact with immutable evidence links (**R4** — "documented finding or report"). The artifact is not a disconnected PDF. It is a record in the `Artifact` table pointing to `Finding`, `Evidence`, and `PolicyClause` records.

**What the copilot does NOT replace:** The MLRO's sign-off (step 6). That is legally required human judgement with personal liability. The copilot surfaces the "attest" action; a human executes it. The system cannot transition an artifact to `filed` status.

---

## Target User 2: The Fraud Operations Analyst

**Who they are:** More junior than the compliance officer, typically 1-5 years experience. Higher volume, faster decisions. Often working in shifts for real-time coverage. Their job is triage speed — correctly routing alerts to "close" or "investigate" within SLA (often 30 minutes per alert).

### Process C: Real-time fraud alert triage

**Current workflow:**

1. Alert queue in the fraud monitoring system. Volume: 200-500 alerts per analyst per day at a mid-size bank. False positive rate: 90-98%.
2. Open an alert. See: "Card transaction ₹89,000 at merchant 'ELECTRONICS STORE' in Mumbai. Cardholder home city: Chennai. Rule: GEO_DEVIATION."
3. Check: has the cardholder transacted in Mumbai before? Open CBS, search transaction history. Scroll. Yes, they travelled there last month — 3 hotel charges. Probably legitimate.
4. Close alert. Write disposition: "Cardholder has prior Mumbai transaction history. No action." Time: 3-5 minutes.
5. Next alert. "Account received ₹4.95L, then ₹4.90L, then ₹4.85L from three different accounts within 2 hours. Rule: STRUCTURED_CREDITS_BELOW_THRESHOLD."
6. Check counterparty accounts in CBS. All three are recently opened (< 30 days). Low balances except for a single large inward transfer each, immediately forwarded to the flagged account. Classic pass-through pattern.
7. This needs investigation. Escalate to L2 with a note. But the analyst cannot easily check: do these 3 accounts share any common attribute (phone, address, IP, device fingerprint)? That data lives in the onboarding system, which the fraud analyst may not even have access to.
8. L2 investigator repeats the data gathering. If the pattern confirms, opens a fraud case. The case file is assembled manually from screenshots and Excel exports.

**What the copilot replaces:**

- Step 3 is **R1** — the copilot joins transaction history with geo data and presents prior activity in the same city alongside the alert.
- Step 7 is the critical gap. The graph query over `PartyRelationship` surfaces shared attributes across the three accounts instantly — shared phone, shared device fingerprint, shared IP at onboarding, fund-flow links. This is **R4** — the signal (structured credits) leads to evidence (network analysis) in one step.
- Step 8 is **R4** + **R5** — the case file is assembled as an evidence bundle with point-in-time references, not screenshots. When the case eventually becomes an STR (it crosses into AML), the evidence is already structured and citable.

---

## Target User 3: The Credit Risk Analyst

**Who they are:** Typically in the CRO's office or a dedicated credit risk team. Responsible for portfolio monitoring, staging, provisioning, and regulatory returns on asset quality. Works at portfolio level (thousands of accounts), not individual transaction level.

### Process D: DPD monitoring, SMA staging, and provisioning

**Current workflow:**

1. Daily or weekly: receive an extract from the LMS (Loan Management System) with current DPD (Days Past Due) for every loan account.
2. Load into Excel or an internal MIS tool. Bucket accounts: SMA-0 (1-30 DPD), SMA-1 (31-60 DPD), SMA-2 (61-90 DPD), NPA (>90 DPD, sub-standard/doubtful/loss by ageing).
3. Compare to prior period. Which accounts migrated between buckets? Which cured? Which slipped?
4. For SMA-2 and new NPAs, pull individual account details. Check: is there a restructuring in place? Is there collateral? What is the security coverage?
5. Compute provisions under Ind AS 109 ECL model: Stage 1 (performing, 12-month ECL), Stage 2 (significant increase in credit risk, Lifetime ECL), Stage 3 (credit-impaired/NPA, Lifetime ECL net of collateral). The staging criteria and PD/LGD parameters live in a spreadsheet maintained by the risk modelling team.
6. Prepare the asset quality report for ALCO / Board Risk Committee.
7. Prepare regulatory returns (RBI supervisory returns on asset quality).

**What the copilot replaces:**

- Steps 1-3 are **R1** + **R4** — the copilot runs staging as a dynamic table over the exposure data, refreshed daily. Migration matrices are a verified query, not a manual Excel comparison.
- Step 4 is the **cross-silo join**. The credit analyst asks about an SMA-2 account and the copilot surfaces: this borrower also has an open fraud case (from Process C) and an AML alert (from Process A). Today, the credit analyst would never know this.
- Steps 5-7 are **R5** — the provisioning computation and the regulatory return are artifacts produced from verified queries, with every number traceable to its source.

---

## Target User 4: The Treasury / ALM Officer

**Who they are:** Treasury middle office, reporting to the CFO or a dedicated ALM function. Responsible for ensuring the institution can meet its obligations as they come due.

### Process E: LCR computation and structural liquidity reporting

**Current workflow:**

1. Monthly (or more frequently under stress): extract balance sheet positions from CBS and the general ledger.
2. Classify each position into the LCR framework: HQLA (Level 1/2A/2B with prescribed haircuts), Cash outflows (by category with prescribed run-off rates), Cash inflows (expected repayments with caps).
3. Apply the regulatory weights and haircuts from Basel III / RBI framework.
4. Compute: LCR = HQLA ÷ Net Cash Outflows (30-day stress). Must be ≥ 100% (or applicable RBI minimum).
5. For structural liquidity: bucket all assets and liabilities by residual maturity, compute cumulative gap per bucket, check against tolerance limits.
6. Behavioural assumptions (what fraction of demand deposits actually leave within each bucket) are maintained in a separate document, approved by ALCO, applied as overrides.
7. Prepare the ALM statement for ALCO and the RBI return.

**What the copilot replaces:**

- Steps 2-4 are **R1** + **R3** — the LCR computation is a verified query over a semantic view. Regulatory haircuts and run-off rates are policy data in `PolicyClause` with effective dates.
- Step 5 is **R5** — the structural liquidity statement is an artifact.
- Step 6 is **R1** — behavioural assumptions are a governed object, version-controlled and effective-dated.

---

## Data Sources — Complete Map

| Source | What we ingest | What it replaces |
|---|---|---|
| **Core Banking System** | Transaction feed (Snowpipe Streaming for real-time, batch for history), account master, customer master | Manual CBS lookups during investigation |
| **Loan Management System** | Exposure data: outstanding, limit, DPD, repayment schedule, collateral | Manual LMS extracts into Excel |
| **KYC/CDD platform** | Customer risk rating, PEP status, CDD records, beneficial ownership | Manual KYC lookups during investigation |
| **General Ledger** | Balance sheet positions by GL code | Manual GL extracts for ALM |
| **Sanctions lists (OFAC, UN, MHA)** | Structured watchlist entries, refreshed daily | Vendor sanctions screening tool |
| **RBI circulars / Master Directions** | Policy text → parsed into structured `PolicyClause` records | Compliance officer manually reading PDFs and updating internal policy docs |
| **FIU-IND reporting formats** | Filing templates and field definitions | Officer manually filling Word/Excel templates |
| **Case management system** | Replaced entirely by `Case`, `Evidence`, `Finding`, `Artifact`, `Attestation` tables | Jira/ServiceNow/email/shared drives |

**What we do NOT ingest** (explicit):
- Credit bureau data (commercial, licensed — note as integration point)
- Real customer data (entire dataset is synthetic — a strength, not a gap)
- Payment network data (NPCI/SWIFT raw feeds — simulated)

---

## Summary: The Replacement Map

| PS requirement | Current process | Current pain | What the copilot delivers |
|---|---|---|---|
| **R1** "Combine transaction and account data with policy and filing text" | Officer logs into 4-6 systems, manually cross-references | 70-80% of investigation time is data gathering | One semantic layer joining structured data with parsed policy rules |
| **R2** "Let a business or compliance user ask questions" | Officer writes no queries; relies on canned reports and manual lookups | Cannot ask ad-hoc questions without IT support | Natural language over verified queries — deterministic answers from governed SQL |
| **R3** "Governed, explainable, evidence-backed answers" | Numbers are manually transcribed; no provenance chain | "Where did this number come from?" is unanswerable after the fact | Every answer cites query ID, source rows, as-of timestamp, and applicable policy clause |
| **R4** "Signal to evidence to documented finding" | Alert → manual investigation → Word document with no data lineage | The chain exists only in the officer's memory and their Excel files | Persisted pipeline: Signal → Case → Evidence → Finding → Artifact → Attestation |
| **R5** "Audit-ready regulatory outputs" | Officer manually assembles returns in Excel/Word; MLRO reviews prose with no automated cross-check | Revision cycles, transcription errors, stale policy references, no reproducibility | Agent-drafted artifacts with numeric reconciliation gate; every number re-verifiable via Time Travel |

**Key insight:** The users are not looking for a faster dashboard. They are looking to stop spending 80% of their time gathering data and transcribing numbers, so they can spend that time on the judgement calls they are actually paid and legally accountable for.
