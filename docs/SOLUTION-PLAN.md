# Risk & Compliance Copilot — Solution Plan

**Problem statement:** Banking/NBFC teams manage real-time fraud, liquidity and credit risk, and
regulatory reporting (AML, Basel, local regulations) largely manually today. Build a copilot that
surfaces risk and fraud signals and produces audit-ready regulatory outputs from natural language
questions.

**Judging focus:** Real World Relevance · Technical Execution · Solution Completeness
**Platform requirement:** CoCo (CLI/Desktop) used across planning, development, execution, and testing.

| | |
|---|---|
| Status | Draft — under revision |
| Last updated | 2026-09-19 |
| Owner | — |

> **Verification flag.** Domain structure and typologies below are written from general knowledge.
> **Exact thresholds, form numbers, filing deadlines, and feed URLs change and must be verified**
> against current RBI / FIU-IND / BCBS / OFAC sources before being hardcoded. See §8.

---

## Contents

1. [What the prompt is actually asking for](#1-what-the-prompt-is-actually-asking-for)
2. [Domain research: the four pillars](#2-domain-research-the-four-pillars)
3. [Architectural principles](#3-architectural-principles)
4. [Data model and ontology](#4-data-model-and-ontology)
5. [Data synthesis approach](#5-data-synthesis-approach)
6. [External inputs and refresh cadence](#6-external-inputs-and-refresh-cadence)
7. [Standout features, ranked](#7-standout-features-ranked)
8. [Scope and the demo spine](#8-scope-and-the-demo-spine)
9. [Research sources](#9-research-sources)
10. [Failure modes to design against](#10-failure-modes-to-design-against)
11. [Build sequence](#11-build-sequence)
12. [Open questions](#12-open-questions)

---

## 1. What the prompt is actually asking for

Read the requirement bullets closely and note what they do *not* ask for:

> "governed, explainable, evidence backed answers"
> "from signal to evidence to a documented finding or report"
> "audit ready regulatory outputs"

This is not natural-language BI. It asks for a system whose **unit of output is a defensible
artifact** — something a compliance officer could hand to a regulator and survive questioning on.

That has a hard technical consequence. In BI, the answer *is* the deliverable. In compliance, the
answer is worthless without the chain behind it: which rule fired, on which rows, under which policy
clause, computed at which point in time, reviewed by whom.

**The chain is the product. The chat is the interface.**

Core mental model:

```
signal  →  case  →  evidence bundle  →  finding  →  artifact  →  attestation
(detect)  (group)   (gather + freeze)  (reason+cite) (draft)    (human sign-off)
```

Every stage is a **persisted object with an ID**, not a step in a prompt chain. If a stage exists
only inside an LLM context window, it is not auditable.

---

## 2. Domain research: the four pillars

Enough domain literacy that the vocabulary is right. For each pillar, the artifact matters most —
the artifact is what we generate.

### 2.1 Fraud — real-time, transaction-level

Typologies, roughly ordered by demo value:

- **Mule networks** — accounts opened to receive and rapidly forward illicit funds. Signals:
  fan-in/fan-out ratio, pass-through velocity (in and out within minutes, near-zero retained
  balance), shared device/IP/phone/address across nominally unrelated accounts. **This is a graph
  problem** — relevant to our depth story.
- **Account takeover (ATO)** — credential compromise. Signal is behavioural discontinuity: new
  device + new geography + beneficiary added + limit raised + drain, in a short window.
- **Structuring / smurfing** — breaking amounts into sub-threshold chunks. Many transactions
  clustered just below a known reporting threshold.
- **First-party / synthetic identity fraud** — the borrower is the fraudster. Bust-out pattern:
  build good behaviour, max out, vanish.
- **Origination / loan fraud** — falsified income docs, ghost borrowers, round-tripping between
  related parties.

*Artifact:* internal fraud case file + regulatory fraud reporting (India: Fraud Monitoring Returns
to RBI — verify current form and timeline).

### 2.2 AML — entity-level, periodic + event-driven

Risk-based regime: **KYC/CDD** at onboarding, **EDD** for high-risk (PEPs, high-risk jurisdictions,
complex ownership), **ongoing monitoring**, **sanctions/PEP screening**, **reporting**.

Under Indian law (PMLA), reporting goes to **FIU-IND**. Distinct report types: **CTR** (cash above
threshold), **STR** (suspicious transaction report), **CCR** (counterfeit currency), **NTR**
(non-profit transactions), **CBWTR** (cross-border wire transfers).

**The STR is the money shot.** It is narrative, evidence-backed, genuinely hand-written today, and
exactly "audit-ready regulatory output from natural language questions."

STR narrative structure to learn: who; which accounts; what pattern over what period; quantified;
why it is suspicious; what the institution did; what supports it.

Also model the distinction between an **alert**, a **case**, and a **filing decision** — and that
most alerts correctly close with no action. A system that files on everything is broken. The
**"no STR warranted, and here is why"** path is as important as the filing path.

### 2.3 Liquidity risk — as-of-date, balance-sheet level

- **LCR** — high-quality liquid assets ÷ net cash outflows under a 30-day stress scenario.
- **ALM / structural liquidity** — assets and liabilities bucketed by maturity, cumulative gap
  limits per bucket. The classic NBFC failure mode: borrow short, lend long, get caught.
- **Behavioural vs contractual maturity** — deposits do not all leave on contract date. These
  assumptions are a governance object in their own right.

*Artifact:* structural liquidity statement with bucketed gaps; LCR computation with component
breakdown; breach explanation.

### 2.4 Credit risk — portfolio-level, daily staging

- **DPD buckets and SMA staging** — SMA-0/1/2 as early warning before NPA.
- **NPA classification** — substandard → doubtful → loss, with prescribed ageing.
- **ECL under Ind AS 109** — Stage 1/2/3; 12-month vs lifetime expected loss; staging driven by
  "significant increase in credit risk."
- **Basel** — Pillar 1 minimum capital (credit + market + operational RWA), Pillar 2 supervisory
  review / ICAAP, Pillar 3 disclosure. For Indian NBFCs the headline analogue is **CRAR**, with a
  Tier 1 sub-minimum, under RBI's Scale-Based Regulation layers.

*Artifact:* provisioning/staging report; CRAR computation; large-exposure and early-warning reporting.

### 2.5 The unifying insight

These four are normally built as four silos, and that **is** the institutional pain:

> The same entity is a fraud signal to one team, an AML subject to another, a credit deterioration
> to a third, and an exposure line to a fourth — and nobody joins them.

That is our demo spine (§8).

---

## 3. Architectural principles

Five non-negotiable decisions.

### 3.1 Deterministic numbers, generated narrative

**The LLM must never compute a regulatory figure.** Not LCR, not CRAR, not a transaction total, not
an alert count. Every number comes from SQL over governed tables — semantic views with verified
queries, so the same question always produces the same SQL.

The LLM's job: select, sequence, explain, cite, write prose. It consumes numbers as inputs it is
forbidden to alter.

Then add the step almost nobody adds: a **programmatic reconciliation check** that parses numerals
out of the generated narrative and asserts each appears in the underlying result set. If a number in
the prose is not traceable to a query result, the artifact is rejected and regenerated.

Ten lines of code; the single most credible thing to show a judge with a risk background.

### 3.2 Point-in-time discipline

Every finding is stamped with `as_of_timestamp` and the query that produced it. Snowflake closes the
loop in a way most platforms cannot: **Time Travel lets us re-execute the exact query
`AT(TIMESTAMP => <as_of>)` and demonstrate the finding reproduces.**

That is a genuine audit-ready capability, not a claim. **Demo it explicitly** — regenerate an old
finding live and show byte-identical numbers.

Also: make the findings table append-only (supersede via revisions, never update) so the audit trail
cannot be quietly rewritten.

### 3.3 Policy text as a control, not as context

- **Weak version:** RAG over policy PDFs, paste a paragraph in as "context."
- **Strong version:** extract policy into **structured, effective-dated rules** —
  `{clause_id, source_doc, page, effective_from, applicability, metric, operator, threshold, action}`.

Then:
- detection rule and narrative citation reference the **same** clause ID
- a threshold change is a data change, not a code change
- we can answer "which controls implement clause X?" and "which findings depend on a clause that
  just changed?"

This is policy-as-code *derived from* policy-as-text, and it enables the best standout feature (§7.2).

Tools: `AI_PARSE_DOCUMENT` for layout-aware extraction, `AI_EXTRACT` for structured fields.
Relevant skills: `document-intelligence`, `ai-functions-pipeline-builder`.

### 3.4 Two-speed architecture, stated explicitly

Fraud is seconds. Basel is as-of-date. Do not pretend they share a pipeline.

- **Fast lane** — Snowpipe Streaming → streams → low-target-lag dynamic tables for transaction
  scoring and velocity features.
- **Slow lane** — batch dynamic tables at daily/monthly lag for balance-sheet aggregation, staging,
  returns.
- **Shared** — the same conformed party/account dimension. Both lanes resolve to one `party_id`.

Saying "these have different SLAs and here is why" is a maturity signal. Bonus: make a fast-lane
fraud signal materialise into the slow lane's exposure view — the join nobody does.

### 3.5 Maker-checker is a schema requirement

Regulatory filings legally require human sign-off with segregation of duties. Model it:

```
finding:     draft → under_review → approved → filed   (+ rejected, withdrawn)
attestation: finding_id, attestor, role, timestamp, comment, signature_hash
```

The agent proposes. A human attests. **The agent cannot transition to `filed`.**

Ten minutes of work; communicates more banking credibility than any feature.

---

## 4. Data model and ontology

Design before generating a row. Draft it in CoCo and keep the artifact — it is planning-phase evidence.

| Entity | Why it exists |
|---|---|
| `Party` | Customer, counterparty, beneficial owner. Risk rating, PEP flag, KYC status, jurisdiction. Entity-resolution anchor. |
| `PartyRelationship` | Party↔Party: ownership, shared device/IP/address/phone, fund flow. **Enables ring detection.** |
| `Account` | Owned by party; product type, balance, status. |
| `Transaction` | The spine. Debit/credit, channel, counterparty account, timestamp, amount, geo, device. |
| `Exposure` | Credit position: outstanding, limit, DPD, stage, collateral. |
| `Signal` | One rule/model firing on one entity at one time. Cheap, high-volume. |
| `Case` | Groups signals into an investigable unit. Owner, status, SLA clock. |
| `Evidence` | **Pointers, not copies:** `(case_id, ref_type, ref_id, query_id, as_of)`. |
| `Finding` | Reasoned conclusion. Cites evidence + policy clause. Versioned. |
| `PolicyClause` | Extracted rule from policy/circular text. Effective-dated. |
| `Artifact` | Generated output: STR draft, LCR statement, board memo. Links to finding. |
| `Attestation` | Human sign-off. Append-only. |

### 4.1 The critical modelling choice

**`Evidence` stores references, not values.** A copied number is a snapshot you must trust. A
`(txn_id, as_of_ts, query_id)` triple is **re-verifiable** — which is the literal definition of
audit-ready.

### 4.2 Entity resolution

The whole value proposition collapses if the fraud team's customer and the credit team's borrower are
different rows. Build a deliberate `party_id` resolution step, document the match strategy, and make
uncertainty visible (**match confidence as a column**). Judges will ask.

### 4.3 Graph traversal

Mule-ring and related-party detection is graph traversal. Snowflake handles this with **bounded
recursive CTEs** over `PartyRelationship`. A depth-3 fund-flow ring with a shared-device edge is a
legitimately sophisticated query and a strong visual. Bound the depth; guard against cycles.

### 4.4 Semantic layer

Author **one semantic view per domain** (transactions/AML, exposures/credit, balance-sheet/liquidity)
— not one god-view. Then invest heavily in **verified queries** for every regulatory metric: CRAR,
LCR, SMA counts, threshold-breach counts.

Verified queries are how "deterministic numbers" becomes real: the metric definition lives in the
semantic layer, version-controlled, not in a prompt. Skill: `agent-studio`.

---

## 5. Data synthesis approach

### 5.1 Decision: calibrated agent-based simulation with planted typologies

**Rejected: SMOTE.** Not a data generator. It interpolates minority-class *feature vectors* to
rebalance a training set. No concept of a graph (cannot create a mule ring), no concept of
double-entry (cannot keep a balance sheet tied), and interpolation can produce physically impossible
records. If used at all, it belongs inside a classifier's training step, never in dataset construction.

**Rejected: tabular GANs (CTGAN, TVAE).** Fatal premise problem — they learn a distribution from
*real* data, and we have none by design. Training a GAN on data we fabricated is a laundering step
that adds zero information and **destroys the one asset we need: known ground truth.** Also: no
cross-table referential integrity, no graph topology, **mode-collapse precisely on rare events (and
fraud *is* the rare event)**, and unexplainable — disqualifying when a judge asks "why is this
account suspicious?"

**Chosen: agent-based simulation.** What the field actually does — the published simulators in this
domain (PaySim, AMLSim) are agent-based, not generative.

### 5.2 Comparison table (for the deck)

| Criterion | SMOTE | Tabular GAN | **Agent-based simulation** |
|---|---|---|---|
| Needs real training data | yes | **yes** | **no** |
| Ground-truth labels | partial | **none** | **exact, by construction** |
| Referential integrity across tables | no | no | **enforced** |
| Balance sheet / double-entry ties | no | no | **enforced** |
| Graph topology (mule rings) | no | no | **designed explicitly** |
| Rare-event fidelity | distorts | mode-collapses | **specified directly** |
| Explainable to an auditor | no | no | **yes — documented rationale** |
| Reproducible | partial | hard to pin | **fixed seed → identical data** |
| Build time | low | high (train + tune) | **low** |

Rows 3–6 are where the generative approaches fail and where this problem lives. Row 2 is the killer:
**without labels there is no precision/recall, no confusion matrix, no "Testing and validation" score.**

### 5.3 The honest concession, and its mitigation

Agent-based simulation is only as statistically rich as its assumptions. Do not hide this —
**calibrate parameters from published aggregate statistics** rather than inventing them: amount
distributions, channel mix, time-of-day curves, DPD roll rates, sector NPA ratios, payment-rail
volume mixes. RBI payment-system data, NPCI volume stats, and published industry NPA/provisioning
ratios are public aggregates.

This upgrades the claim from "synthetic data" to **"calibrated simulation."**

### 5.4 Where generative AI does belong

The **unstructured layer** — policy documents, circular text, KYC narratives, complaint text, prior
STR narratives. There, language variety *is* the value and there are no numbers to corrupt. Use
`AI_COMPLETE`, conditioned on the structured facts already planted, so text and data agree.

**Line for the deck:** *generative models where language variety adds value; deterministic simulation
where numbers and relationships must be provable.*

### 5.5 The hybrid architecture

| Layer | Method |
|---|---|
| Structured spine | deterministic seeded simulator |
| Typologies | planted, with held-out `ground_truth` table |
| Parameters | calibrated from public aggregate statistics |
| Unstructured layer | `AI_COMPLETE`, conditioned on planted structured facts |
| Validation | reconciliation tests (hard gate) + statistical plausibility checks |

### 5.6 Ground truth and measurement

For each typology, plant a known count and record it:

```
ground_truth: entity_id, typology, planted_at, severity, expected_detection
```

This enables precision, recall, and a confusion matrix over the detection logic — **the entire
"Testing and validation" score**, and it converts "we built a thing" into "we built a thing and here
is how well it works."

### 5.7 Three things teams get wrong

1. **Base rates.** Real fraud is a fraction of a percent. At 30% everything looks easy and nothing is
   credible. Keep it honest so the metrics mean something.
2. **Referential consistency.** Balances must reconcile to transactions; exposures must roll up to
   portfolio totals; maturity buckets must sum to the balance sheet. If LCR does not tie to our own
   balance sheet, a judge who checks will find it. **Reconciliation suite runs as a gate.**
3. **Hard negatives.** Plant lookalikes that should *not* fire: a legitimate business with high cash
   velocity, a customer who legitimately travels, a seasonal borrower who legitimately goes 45 DPD
   and cures. Without these, precision is fake.

Also plant a **temporal narrative**, not just static anomalies: an account that looks normal,
degrades over weeks, then bursts. Point-in-time correctness only demonstrates itself against data
that changes over time.

### 5.8 Privacy note

Differential privacy and k-anonymity are for when you *hold* real data and must release it safely.
Our answer is stronger: **production data was never ingested, so the risk is eliminated rather than
mitigated.**

---

## 6. External inputs and refresh cadence

### 6.1 Decision: regulation is not a real-time feed

**Do not build, or claim, real-time regulatory ingestion.** It signals to a domain judge that we do
not understand the tempo of the work.

Regulatory change has a different clock: circulars publish irregularly, carry an **effective date
usually in the future**, and require **human legal interpretation** before becoming a control change.
Auto-activating a parsed threshold would be a compliance incident, not a feature.

### 6.2 Multi-clock design

| Layer | Clock | Mechanism |
|---|---|---|
| Transactions / fraud signals | seconds–minutes | Snowpipe Streaming → streams → low-lag dynamic tables |
| **Sanctions / PEP / watchlists** | **daily, or on publication** | scheduled task — these genuinely change often |
| Regulatory text (circulars, master directions) | **daily poll, human-gated promotion** | task → parse → diff → impact → review queue → approve → effective-dated activation |
| Prudential metrics (LCR, CRAR) | as-of-date | batch dynamic tables |
| Returns / filings | statutory calendar | scheduled task aligned to due dates |

**Critical rule:** the policy pipeline outputs a **proposed** rule change, never an activated one.
Ingest → extract → diff → impact analysis → compliance review queue → human approval → activation on
the clause's effective date. Same maker-checker principle as §3.5, applied to the policy layer — and
the review queue *is* the demo for §7.2.

### 6.3 Real public sources worth wiring

> Listed from knowledge. **Verify current URLs, formats, and terms of use before building.**

**Best real external input: sanctions lists.** Genuinely public, machine-readable, updated on change,
and genuinely used in AML screening:

- **UN Security Council Consolidated List** — XML, updated on change
- **OFAC SDN + consolidated lists** (US Treasury) — XML/CSV, well documented, frequent
- **EU consolidated sanctions list**; **UK OFSI consolidated list**
- **MHA UAPA** designated individuals/organisations (India)

Ingesting the real UN or OFAC list is a strong credibility move — actual regulatory data inside an
otherwise synthetic demo, free and legal to use.

**Scheduled, low-frequency, high-value:**
- **FATF high-risk and monitored jurisdictions** — updated ~3×/year at plenaries. Perfect scheduled
  job; drives real risk-scoring logic.
- **RBI** circulars/notifications and Master Directions — feeds exist; verify endpoints. Source for
  the circular watcher.
- **FIU-IND** reporting formats and guidance.
- **RBI reference rates** — daily, if revaluation is in scope.

**Adjacent borrower-risk signals (optional depth):**
- MCA / IBBI insolvency filings against borrowers
- CERSAI security-interest registry
- Adverse-media screening — a real AML requirement; public news APIs work
- Credit bureau data is **commercial, not public** — note rather than pretend

**Check before scraping:** the Snowflake Marketplace carries sanctions, PEP, firmographic, and
adverse-media listings. Pulling a share beats scraping and is a legitimate ingenuity point.

### 6.4 Note on automation placement

`cortex automation` runs in a Snowflake-managed sandbox **with no local filesystem access**. That
suits this use case exactly: a circular watcher or sanctions refresh should write to Snowflake stages
and tables, not to a local repo.

---

## 7. Standout features, ranked

Mapped to the brief's bonus list, ordered by differentiation per unit effort.

### 7.1 Adversarial reviewer agent — highest payoff

A final agent whose only job is to attack the artifact before a human sees it:

- Does every numeral in the narrative trace to a query result?
- Does every assertion cite evidence?
- Is every cited policy clause in effect on the as-of date?
- Is the evidence sufficient, or should this be "insufficient evidence"?
- Were contradictory signals ignored?

On failure the artifact is regenerated or downgraded to "needs analyst review."

Scores simultaneously on **multi-agent orchestration**, **guardrails and graceful fallback**, and
**testing/validation**. Best ROI on the entire list.

### 7.2 Regulatory change management — most differentiated

Drop a new circular into a stage. The pipeline parses it, extracts changed clauses, diffs against
`PolicyClause`, then reports: **which controls are affected, which open findings relied on the
superseded clause, and what the numbers look like re-run under the new rule.**

A genuine, expensive, universal banking pain point; the natural payoff of §3.3 and §6.2. Run as a
scheduled automation to also cover unattended operation. Unlikely any other team does this.

### 7.3 One entity, four lenses

The demo structure — see §8.2. Scores directly on Solution Completeness.

### 7.4 MCP connectors chosen for workflow fit

Wire the ones that complete the loop, not the ones that are easy:

- **Jira** — the case *is* a ticket. Opening an investigation creates it with the evidence bundle
  attached. This is how case management actually works.
- **Slack** — alert the compliance channel; run maker-checker approval in-thread. Strong visual.
- **Google Drive / SharePoint** — where circulars and filed reports actually live. Ingest from there;
  write the final PDF back.

Three connectors each serving a distinct workflow stage beats six that only read data.

### 7.5 Custom tools that take action

Register functions, not just retrieval: `open_case()`, `attach_evidence()`, `request_attestation()`,
`recompute_metric(as_of)`, `flag_account_for_review()`.

Stub the genuinely destructive ones (account freeze) behind an obvious mock **and say so** — that is
a governance point in our favour, not a gap.

### 7.6 Guardrails, concretely

Beyond the reviewer:
- PII masking policies with role-based unmasking — demo an analyst seeing masked data and a
  supervisor unmasking, with access logged
- **"insufficient evidence" as a first-class terminal outcome**, not an error
- confidence thresholds routing to human review rather than guessing
- read-only session scoping so the agent provably cannot mutate source data

### 7.7 Reusable skills

Three or four genuinely reusable, clearly documented skills. Note all are **generic** — none is "our
demo, wrapped." That is the bar.

- `policy-to-rule-extractor` — circular/policy PDF → structured effective-dated rules
- `evidence-bundle-builder` — case ID → re-verifiable evidence pack with point-in-time query refs
- `regulatory-narrative-drafter` — finding + evidence → cited narrative, with the numeric
  reconciliation gate built in
- `planted-typology-generator` — synthetic data with ground-truth labels

### 7.8 Surfaces

CLI for the engineering/orchestration story, Streamlit for the compliance user, Slack for
alert-to-approval, Snowsight for the cloud-agent angle. **Same semantic layer and same findings
tables behind all of them** — that is the point to land: one governed core, many surfaces.

---

## 8. Scope and the demo spine

### 8.1 Scope decision

Four domains built shallowly reads as thin.

- **Deep: Fraud + AML.** They share the transaction spine and the network/graph angle, so the
  marginal cost of the second is low, and AML yields the best artifact (STR).
- **One prudential metric, done properly: LCR *or* CRAR** — not all of Basel. One is enough to prove
  the balance-sheet regulatory-output axis. Pick whichever the synthetic balance sheet supports most
  cleanly.
- **Credit risk as the bridge.** SMA/DPD staging is cheap and is the connective tissue — it feeds
  both provisioning and the capital metric.

### 8.2 The demo spine — one entity, four lenses

> A single synthetic counterparty. Fraud signals fire on pass-through velocity and a shared-device
> link to three other accounts. The investigation escalates to an AML case; the ring is traced with a
> graph query; the agent drafts a cited STR and the reviewer agent validates every number before a
> human attests. The same counterparty is a borrower — the fraud finding triggers early-warning
> review, credit staging moves, provisioning changes, and the exposure flows through to the CRAR
> computation, which now sits closer to its floor. The agent explains the chain in one narrative,
> citing the transactions, the policy clauses, and the as-of timestamps.

**One entity, one story, four pillars, one evidence chain.** Far stronger than four disconnected
features, and it *is* the argument for why joining these silos matters — the Real World Relevance score.

---

## 9. Research sources

### 9.1 Regulatory primary sources
> Verify current versions — these get revised.

- RBI **Master Directions for NBFCs** under Scale-Based Regulation — layer definitions, CRAR and
  Tier 1 minimums, applicability thresholds
- RBI **Master Direction on Fraud Risk Management** (consolidated, revised 2024) — classification,
  reporting timelines, governance
- RBI **IRAC norms** — DPD, SMA-0/1/2, NPA ageing
- RBI **ALM guidelines** — maturity buckets, cumulative gap tolerances
- **PMLA + PML Rules**, and **FIU-IND** reporting formats — CTR/STR/CCR/NTR/CBWTR structure
- **BCBS Basel III** framework and **Pillar 3 disclosure templates** — structure and vocabulary
- **Ind AS 109** — ECL staging criteria

### 9.2 Typology and control material

- **FATF** 40 Recommendations + typology reports — canonical money-laundering vocabulary
- **Wolfsberg Group** statements and questionnaires — precise control language to lift into the
  policy model
- **ACAMS** typology material — practitioner-level red-flag catalogues

### 9.3 Snowflake capability research
Via `cortex search docs` and the relevant skills.

- dynamic tables (incremental refresh, target lag), streams, tasks, Snowpipe Streaming
- Cortex Analyst semantic views + verified queries
- `AI_PARSE_DOCUMENT`, `AI_EXTRACT`, `AI_CLASSIFY`, `AI_COMPLETE`
- Time Travel for point-in-time reproduction
- masking policies, row access policies, tags/classification
- recursive CTE patterns and their limits

---

## 10. Failure modes to design against

- **Chat-over-tables masquerading as a copilot** — no evidence chain, no artifact, no sign-off.
- **The LLM computing regulatory figures** — fatal with any judge who knows the domain.
- **No as-of discipline** — numbers shift between demo runs and cannot be explained.
- **Policy RAG returning paragraphs** instead of clause-level, effective-dated rules.
- **Skipping maker-checker** — an agent that files autonomously is, to a compliance audience, a
  compliance *incident*.
- **No measured accuracy** — "it works" without a confusion matrix is unverifiable.
- **Vanity multi-agent** — five agents passing text scores worse than three with distinct
  responsibilities and clean handoffs.
- **Perfect-path-only demo** — show the refusal, show "insufficient evidence," show a false positive
  correctly closed with documented rationale. Graceful failure is on the scoring list explicitly and
  is the easiest place to stand out, because nobody rehearses it.
- **Claiming real-time regulatory ingestion** — see §6.1.
- **Using a GAN to generate the structured data** — destroys ground truth; see §5.1.

---

## 11. Build sequence

Generate CoCo evidence as a by-product rather than reconstructing it later: keep planning transcripts,
the ontology draft, semantic-view validation output, reconciliation test runs, the precision/recall
report, and automation definitions.

| # | Step | Notes |
|---|---|---|
| 1 | **Ontology + evidence-chain schema** | First. `Evidence`-as-pointers is load-bearing for everything downstream. |
| 2 | **Choose and document the synthesis approach** | Explicit and written down — it is a slide. Record the reasoning against GAN/SMOTE. |
| 3 | **Build the simulator and the data** | See breakdown below. |
| 3a | — calibrate parameters from public aggregate statistics | §5.3 |
| 3b | — deterministic seeded simulator for the structured spine | fixed RNG seed |
| 3c | — plant typologies, hard negatives, temporal narrative; write `ground_truth` | §5.6, §5.7 |
| 3d | — `AI_COMPLETE` the unstructured layer, conditioned on planted facts | §5.4 |
| 3e | — reconciliation + plausibility tests as a **hard gate** | §5.7 |
| 4 | **Semantic views + verified queries** for every regulatory metric | Validate against NL questions now, not at the end. |
| 5 | **Detection layer** — rules + graph queries | Scored against `ground_truth` from day one. |
| 6 | **Document lane** — policy/circular parsing into effective-dated `PolicyClause` | §3.3 |
| 7 | **External inputs and refresh cadence** | Sanctions/PEP, FATF list, circular watcher. Each on its own clock, **human-gated promotion**. §6 |
| 8 | **Agent chain** — signal → investigate → policy-map → draft → **review** | Build the reviewer early; it changes what the drafter must produce. |
| 9 | **Artifact generation + numeric reconciliation gate + attestation workflow** | §3.1, §3.5 |
| 10 | **Surfaces and MCP** | Last. Shallowest layer, most tempting to start with. |

**Discipline to hold:** do not build the Streamlit app first. It is the most visible layer and the
least load-bearing; time spent there early is time not spent on the evidence chain that differentiates us.

**Steps 2, 3a, and 7 were additions to the original plan.** Step 7 was the real gap — without it the
policy layer is a static one-time load, which quietly kills the regulatory-change-management feature
(§7.2), our strongest differentiator.

---

## 12. Open questions

| # | Question | Status |
|---|---|---|
| 1 | LCR or CRAR as the single prudential metric? Depends on which the synthetic balance sheet supports most cleanly. | open |
| 2 | Does the Snowflake Marketplace carry usable sanctions/PEP/adverse-media listings, or do we scrape? | **to research** |
| 3 | Current RBI circular feed endpoint + format; current OFAC/UN list URLs and schemas. | **to verify** |
| 4 | Exact current values: NBFC CRAR/Tier 1 minimums, LCR applicability thresholds, FIU-IND reporting thresholds, fraud reporting deadline. | **to verify** |
| 5 | Which MCP servers are actually available in the hackathon environment (Jira / Slack / Drive)? | open |
| 6 | Team size and split — who owns simulator vs semantic layer vs agent chain vs surfaces? | open |
| 7 | Demo environment: which Snowflake account/warehouse, and are Cortex AI functions enabled there? | open |
