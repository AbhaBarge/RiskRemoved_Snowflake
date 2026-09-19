# What is an upstream lineage for a dbt model

|  |  |
|---|---|
| Session ID | `b48f8e68-7502-4b92-a394-aef83980001a` |
| First message | 2026-09-18T17:52:37.348Z |
| Last message | 2026-09-18T20:51:24.024Z |
| User turns | 4 |
| Exported | 2026-09-19 02:21:33 |
| Thinking included | False |
| Tool calls included | False |

---

## Turn 1 — User

What is an upstream lineage for a dbt model

### Assistant

Upstream lineage for a dbt model refers to **all the data sources and transformations that feed into that model** — everything "above" it in the data flow.

Concretely, this includes:

- **Sources** — the raw tables/views the model reads from (declared in `sources.yml`)
- **Parent models** — other dbt models referenced via `{{ ref('...') }}` that must run before this one
- **Seeds** — any CSV seed files referenced by the model
- **Snapshots** — any dbt snapshots the model depends on

For example, if model `fct_orders` references `stg_orders` and `stg_customers`, and those staging models each read from raw source tables, the upstream lineage is:

```
raw.orders ──► stg_orders ──┐
                             ├──► fct_orders
raw.customers ► stg_customers┘
```

**How to inspect it:**

- **dbt docs** — `dbt docs generate && dbt docs serve` renders an interactive DAG where you can click any model and trace upstream.
- **`dbt ls`** — `dbt ls --select +fct_orders` lists all upstream dependencies (the `+` prefix means "this model and everything upstream").
- **Snowflake lineage** — if the models are materialized in Snowflake, you can query `SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY` or use `GET_LINEAGE()` to trace which tables feed a given table, independent of dbt metadata.

The upstream lineage is what dbt uses to determine **execution order** — it ensures parent models are built before their children.


---

## Turn 2 — User

What's DAG?

### Assistant

**DAG** stands for **Directed Acyclic Graph**.

- **Directed** — edges (dependencies) have a direction (A feeds into B, not the reverse).
- **Acyclic** — no cycles; you can't follow the edges and end up back where you started.
- **Graph** — a set of nodes (models, sources) connected by edges (dependencies).

In dbt, the DAG represents the dependency order of all your models, seeds, snapshots, and tests. dbt builds it from your `ref()` and `source()` calls, then uses it to determine which models to run and in what order — parents before children, with independent branches running in parallel.


---

## Turn 3 — User

Banking and NBFC teams manage real time fraud, liquidity and credit risk, and regulatory reporting (AML, Basel, and local regulations), largely manual today.

Build a copilot that surfaces risk and fraud signals and produces audit ready regulatory outputs from natural language questions.

Combine transaction and account data with policy and filing text
Let a business or compliance user ask questions and get governed, explainable, evidence backed answers
Cover the flow from signal to evidence to a documented finding or report
Judging Focus
Real World Relevance
Technical Execution
Solution Completeness
Snowflake CoCo Usage Guidelines
All hackathon solutions must use CoCo (CLI, Desktop app) across the full lifecycle, from planning through development to execution and testing. Teams should be able to show CoCo in each phase below, and judges will look for evidence of it at every stage.

Planning: Use CoCo to explore the data, frame the problem, draft the solution design, and outline the data model, ontology, and workflow before any build begins.
Development: Use CoCo to build the pipelines, semantic views, models, agents, and application code, iterating directly in the CLI or Desktop app rather than by hand.
Execution: Run and orchestrate the complete end to end solution through CoCo, including scheduled or automated runs where the scenario calls for it.
Testing and validation: Use CoCo to validate outputs, test accuracy, handle errors and edge cases, and confirm the solution behaves correctly before the demo.
Recommended CoCo tasks to demonstrate

Synthetic data generation: Generate realistic, referentially consistent synthetic or de identified datasets with CoCo so teams never need production data and can respect privacy from the start.
Data pipeline creation: Build and orchestrate ingestion and transformation pipelines (for example dynamic tables, tasks, and streams) directly through CoCo, including incremental and near real time flows.
Semantic model and ontology authoring: Use CoCo to infer and generate semantic views, verified queries, and the ontology from a schema, then validate them against natural language questions.
Streamlit report and app generation: Scaffold and run a Streamlit report or application through CoCo to present insights and let users take action inside the solution.
Connecting to additional sources via MCP: Wire in external systems and tools through MCP (for example Jira, Slack, Google Drive, or other MCP servers) so the agent can read from and act across those tools.
Document and unstructured processing: Parse, extract, and enrich documents with CoCo and combine them with structured data for richer answers.
Ways to showcase ingenuity in CoCo tool usage

Beyond building reusable skills, teams can stand out by using these CoCo capabilities:

Reusable and shareable skills: Publish custom skills or agent skills that other teams could reuse. Clearly documented, reusable skills remain the headline bonus.
MCP connectors to external tools: Extend the solution by connecting CoCo to additional data sources and tools through MCP, turning read only analysis into cross tool action.
Automations and scheduled runs: Use CoCo automations, such as scheduled tasks in the CLI and Desktop app or asynchronous runs, so the solution operates unattended on a schedule.
Custom tools and function calling: Register custom tools or functions the agent can call to take real actions rather than only returning text.
Multi agent orchestration: Coordinate several agents or skills into one workflow, with clear handoffs and shared context.
Working across surfaces: Demonstrate the same solution across the CoCo CLI, Desktop app, and Snowsight Cloud Agents, and surface it through the Slackbot where useful.
Guardrails and graceful fallback: Show validation, error handling, confidence checks, and governed behavior so the agent fails safely and stays trustworthy. Guide me appropriately on the research part of this PS. The implementation can all be done with Cortex Code in a glance. I want to focus on the standout features and get the base right, go in depth starting with an overview while explaining it to me

### Assistant

Good problem choice — it has a real pain point and a natural "artifact" output, which is rarer than it sounds. Let me give you the research framing in layers, because the thing that will decide your placing is a design decision you make in the first hour, not the features you build in the last six.

One honesty flag up front: I'll give you the domain structure and typology detail from knowledge, but **exact thresholds, form numbers, and filing deadlines change** — verify those against current RBI/FIU-IND/BCBS sources before you hardcode them into a demo. I'll point you at what to read.

---

## 1. The overview: what the prompt is actually asking for

Read the three requirement bullets again and notice what they're *not* asking for:

> "governed, explainable, evidence backed answers"
> "from signal to evidence to a documented finding or report"
> "audit ready regulatory outputs"

They are not asking for natural-language BI. They are asking for a system whose **unit of output is a defensible artifact** — something a compliance officer could hand to a regulator and survive questioning on.

That distinction has a hard technical consequence. In BI, the answer *is* the deliverable. In compliance, the answer is worthless without the chain behind it: which rule fired, on which rows, under which policy clause, computed at which point in time, reviewed by whom. **The chain is the product. The chat is the interface.**

Most teams will build chat-over-tables with a nice Streamlit skin and lose on "Solution Completeness" without understanding why. Build the chain first, then put a conversation on top of it.

The mental model I'd hold:

```
signal  →  case  →  evidence bundle  →  finding  →  artifact  →  attestation
(detect)  (group)   (gather+freeze)   (reason+cite) (draft)    (human sign-off)
```

Every one of those is a persisted object with an ID, not a step in a prompt chain. If a stage only exists inside an LLM context window, it isn't auditable.

---

## 2. Domain research: what the four pillars actually mean

You need enough domain literacy that the vocabulary is right. Judges with banking background spot fake fluency instantly. Here's the skeleton of each, plus what the *artifact* is — because the artifact is what you're generating.

### Fraud (real-time, transaction-level)

Typologies worth knowing, roughly in order of how demo-able they are:

- **Mule networks** — accounts opened to receive and rapidly forward illicit funds. Detected by fan-in/fan-out ratio, pass-through velocity (money in, money out within minutes, near-zero retained balance), shared device/IP/phone/address across supposedly unrelated accounts. This is a *graph* problem, which matters for your depth story.
- **Account takeover (ATO)** — credential compromise. Signal is behavioural discontinuity: new device + new geography + beneficiary added + limit raised + drain, within a short window.
- **Structuring / smurfing** — breaking large amounts into sub-threshold chunks to avoid reporting triggers. Detected as many transactions clustered just below a known reporting threshold.
- **First-party / synthetic identity fraud** — the borrower is the fraudster. Bust-out patterns: build good behaviour, max out, vanish.
- **Loan/origination fraud** — falsified income docs, ghost borrowers, round-tripping between related parties.

Artifact: internal fraud case file, plus regulatory fraud reporting (in India, Fraud Monitoring Returns to RBI, with prescribed timelines — verify current form and deadline).

### AML (entity-level, periodic + event-driven)

The regime is risk-based: **KYC/CDD** at onboarding, **EDD** for high-risk (PEPs, high-risk jurisdictions, complex ownership), **ongoing monitoring**, **sanctions/PEP screening**, and **reporting**.

Under Indian law (PMLA), reporting goes to **FIU-IND**, and the report types are distinct — CTR (cash transactions above threshold), STR (suspicious transaction reports), CCR (counterfeit currency), NTR (non-profit transactions), CBWTR (cross-border wire transfers). **The STR is your money shot.** It's narrative, it's evidence-backed, it's exactly "audit-ready regulatory output from natural language questions," and it's genuinely written by hand today.

Learn the STR narrative structure — it's essentially: who, what accounts, what pattern over what period, quantified, why it's suspicious, what the institution did, and what supports it. Writing a *well-structured* STR draft is a much stronger demo than writing a vaguely plausible one.

Also learn the distinction between an **alert**, a **case**, and a **filing decision** — and that most alerts correctly close as no-action. A system that files on everything is a broken system. Your "no STR warranted, here's why" path is as important as the filing path.

### Liquidity risk (as-of-date, balance-sheet-level)

- **LCR** — high-quality liquid assets over net cash outflows in a 30-day stress scenario. Applies to larger NBFCs; thresholds phased in.
- **ALM / structural liquidity** — assets and liabilities bucketed by maturity, with cumulative gap limits per bucket. This is the classic NBFC failure mode: borrow short, lend long, get caught.
- **Behavioural vs contractual maturity** — deposits don't all leave on contract date. Assumptions are a governance object in their own right.

Artifact: structural liquidity statement with bucketed gaps, LCR computation with component breakdown, breach explanation.

### Credit risk (portfolio-level, daily staging)

- **DPD buckets and SMA staging** — SMA-0/1/2 as early warning before NPA.
- **NPA classification** — substandard → doubtful → loss, with prescribed ageing.
- **ECL under Ind AS 109** — Stage 1/2/3 with 12-month vs lifetime expected loss; staging driven by "significant increase in credit risk."
- **Basel** — three pillars: Pillar 1 minimum capital (credit + market + operational risk-weighted assets), Pillar 2 supervisory review/ICAAP, Pillar 3 public disclosure. For Indian NBFCs the analogous headline metric is **CRAR** (capital to risk-weighted assets), with Tier 1 sub-minimum, under RBI's Scale-Based Regulation layers.

Artifact: provisioning/staging report, CRAR computation, large-exposure and early-warning reporting.

### The unifying insight

These four are usually built as four silos, and that's the actual institutional pain: **the same entity is a fraud signal to one team, an AML subject to another, a credit deterioration to a third, and an exposure line to a fourth — and nobody joins them.**

That's your demo spine. More on this in §7.

---

## 3. The architectural principles that win this

These are the five decisions I'd treat as non-negotiable.

### 3.1 Deterministic numbers, generated narrative

**The LLM must never compute a regulatory figure.** Not the LCR, not the CRAR, not a transaction total, not a count of alerts. Every number comes from SQL over governed tables — semantic views with verified queries, so the same question always produces the same SQL.

The LLM's job is: select, sequence, explain, cite, and write prose. It consumes numbers as inputs it is forbidden to alter.

Then add the step almost nobody adds: **a programmatic reconciliation check** that parses the numerals out of the generated narrative and asserts each one appears in the underlying result set. If a number in the prose isn't traceable to a query result, the artifact is rejected and regenerated. That's ten lines of code and it is the single most credible thing you can show a judge with a risk background.

### 3.2 Point-in-time discipline

Every finding is stamped with an `as_of_timestamp` and the query that produced it. Snowflake lets you close the loop here in a way most platforms can't: **Time Travel means you can re-execute the exact query `AT(TIMESTAMP => <as_of>)` and demonstrate the finding reproduces against the data as it stood.**

That is a genuine "audit-ready" capability, not a claim. Build it and demo it explicitly — regenerate a two-week-old finding live and show it produces byte-identical numbers. Also consider immutability on the findings table (append-only, superseding revisions rather than updates) so the audit trail can't be quietly rewritten.

### 3.3 Policy text as a control, not as context

The weak version: RAG over policy PDFs, paste a paragraph into the answer as "context."

The strong version: **extract policy into structured rules.** Parse each circular/policy doc into rows like `{clause_id, source_doc, page, effective_from, applicability, metric, operator, threshold, action}`. Now:

- The detection rule and the narrative citation reference the *same* clause ID.
- A threshold change is a data change, not a code change.
- You can answer "which of our controls implement clause X?" and "which findings depend on a clause that just changed?"

This is policy-as-code *derived from* policy-as-text, and it sets up the best standout feature on the list (§6.2).

Snowflake gives you `AI_PARSE_DOCUMENT` for layout-aware extraction and `AI_EXTRACT` for pulling structured fields out of the text. Use both; the document-intelligence and ai-functions-pipeline-builder skills cover the patterns.

### 3.4 Two-speed architecture, stated explicitly

Fraud is seconds. Basel is as-of-date. Don't pretend they share a pipeline.

- **Fast lane:** Snowpipe Streaming → streams → low-target-lag dynamic tables for transaction scoring and velocity features.
- **Slow lane:** batch dynamic tables at daily/monthly lag for balance-sheet aggregations, staging, and returns.
- **Shared:** the same conformed entity/account dimension. Both lanes resolve to the same `party_id`.

Saying out loud "these have different SLAs and here's why" is a maturity signal. Bonus: build the fast lane so a fraud signal *materialises into the slow lane's* exposure view — that's the join nobody does.

### 3.5 Maker-checker is a schema requirement

Regulatory filings and material findings legally require human sign-off, with segregation of duties. Model it:

```
finding: draft → under_review → approved → filed  (+ rejected, withdrawn)
attestation: finding_id, attestor, role, timestamp, comment, signature_hash
```

The agent proposes. A human attests. The agent **cannot** transition to `filed`. This one design choice communicates more banking credibility than any feature, and it's ten minutes of work.

---

## 4. The data model and ontology

Design this before you generate a row. Sketch it in CoCo and keep the artifact — it's your planning-phase evidence.

**Core entities:**

| Entity | Why it exists |
|---|---|
| `Party` | Customer, counterparty, beneficial owner. Risk rating, PEP flag, KYC status, jurisdiction. The entity-resolution anchor. |
| `PartyRelationship` | Party↔Party: ownership, shared device/IP/address/phone, fund flow. **Enables ring detection.** |
| `Account` | Owned by party, has product type, balance, status. |
| `Transaction` | The spine. Debit/credit, channel, counterparty account, timestamp, amount, geo, device. |
| `Exposure` | Credit-side position: outstanding, limit, DPD, stage, collateral. |
| `Signal` | A single rule/model firing on an entity at a time. Cheap, high-volume. |
| `Case` | Groups signals into an investigable unit. Has owner, status, SLA clock. |
| `Evidence` | **Pointers**, not copies: `(case_id, ref_type, ref_id, query_id, as_of)`. |
| `Finding` | The reasoned conclusion. Cites evidence + policy clause. Versioned. |
| `PolicyClause` | Extracted rule from policy/circular text. Effective-dated. |
| `Artifact` | The generated output: STR draft, LCR statement, board memo. Links to finding. |
| `Attestation` | Human sign-off. Append-only. |

**The critical modelling choice: `Evidence` stores references, not values.** If evidence is a copied number, it's a snapshot you must trust. If it's `(txn_id, as_of_ts, query_id)`, it's *re-verifiable* — which is the literal definition of audit-ready.

**Entity resolution deserves real attention.** The whole value proposition collapses if the fraud team's customer and the credit team's borrower are different rows. Build a deliberate `party_id` resolution step with a documented match strategy, and make its uncertainty visible (match confidence as a column). Judges will ask.

**Graph angle:** mule-ring and related-party detection is fundamentally graph traversal. Snowflake handles this with bounded recursive CTEs over `PartyRelationship`. A depth-3 fund-flow ring with a shared-device edge is a legitimately sophisticated query and a very visual demo. Bound the depth and guard against cycles.

**Semantic layer:** author a semantic view per domain (transactions/AML, exposures/credit, balance-sheet/liquidity) rather than one god-view. Then invest heavily in **verified queries** for every regulatory metric — CRAR, LCR, SMA counts, threshold-breach counts. Verified queries are how you make "deterministic numbers" real: the metric definition lives in the semantic layer, version-controlled, not in a prompt. The agent-studio skill covers authoring and validating these.

---

## 5. Synthetic data: this is where the demo is won or lost

Do not generate "realistic-looking" data. Generate data with **planted ground truth**.

For each typology, plant a known number of instances and record them in a held-out `ground_truth` table:

```
ground_truth: entity_id, typology, planted_at, severity, expected_detection
```

Now you can compute precision, recall, and a confusion matrix over your detection logic, and *show them*. That is your entire "Testing and validation" score, and it converts "we built a thing" into "we built a thing and here's how well it works."

Three things teams get wrong:

1. **Base rates.** Real fraud is a fraction of a percent. If 30% of your transactions are fraudulent, everything looks easy and nothing is credible. Keep it honest — and then your precision/recall numbers actually mean something.
2. **Referential consistency.** Balances must reconcile to transactions. Exposures must roll up to the portfolio total. Maturity buckets must sum to the balance sheet. If your LCR doesn't tie to your own balance sheet, a judge who checks will find it. Build a reconciliation test suite and run it as a gate.
3. **Hard negatives.** Plant *lookalikes* that should NOT fire: a legitimate business with genuinely high cash velocity, a customer who legitimately travels, a seasonal borrower who legitimately goes 45 DPD and cures. Without these your precision is fake. With them, your false-positive handling becomes a feature you can talk about.

Also plant a **temporal narrative**, not just static anomalies: an account that looks normal, degrades over eight weeks, then bursts. Point-in-time correctness only demonstrates itself against data that changes over time.

---

## 6. Standout features, ranked by payoff-to-effort

Their bonus list is explicit. Here's how I'd map it, ordered by what I think actually differentiates.

### 6.1 The adversarial reviewer agent ⭐ highest payoff

A final agent whose only job is to attack the artifact before a human sees it:

- Does every numeral in the narrative trace to a query result?
- Does every assertion cite evidence?
- Is every cited policy clause currently in effect on the as-of date?
- Is the evidence sufficient for the claimed conclusion, or should this be "insufficient evidence"?
- Are there contradictory signals that were ignored?

If it fails, the artifact goes back or downgrades to "needs analyst review." This single component scores on **multi-agent orchestration**, **guardrails and graceful fallback**, and **testing/validation** simultaneously. Best ROI on the entire list.

### 6.2 Regulatory change management ⭐ most differentiated

Drop a new circular into a stage. The pipeline parses it, extracts changed clauses, diffs against the existing `PolicyClause` table, then reports: **which controls are affected, which open findings relied on the superseded clause, and what the numbers look like re-run under the new rule.**

This is a genuine, expensive, universal banking pain point, it's the natural payoff of §3.3, and I'd be surprised if another team does it. Run it as a scheduled automation and it also covers the unattended-operation requirement.

### 6.3 One entity, four lenses

The demo structure (detailed in §7). Scores directly on Solution Completeness.

### 6.4 MCP connectors chosen for workflow fit

Don't wire MCP servers because they're easy — wire the ones that complete the loop:

- **Jira** — the case *is* a ticket. Opening an investigation creates it, with the evidence bundle attached. This is how case management actually works.
- **Slack** — alert the compliance channel, and run the maker-checker approval in-thread. Approval-in-Slack is a strong visual.
- **Google Drive / SharePoint** — where circulars and filed reports actually live. Ingest from there; write the final PDF back.

Three connectors that each serve a distinct workflow stage beats six that just read data.

### 6.5 Custom tools that take action

Register functions, not just retrieval: `open_case()`, `attach_evidence()`, `request_attestation()`, `recompute_metric(as_of)`, `flag_account_for_review()`. Stub the genuinely destructive ones (account freeze) behind an obvious mock and *say* you stubbed it — that's a governance point in your favour, not a gap.

### 6.6 Guardrails, concretely

Beyond the reviewer: PII masking policies with role-based unmasking (so the demo can show an analyst seeing masked data and a supervisor unmasking, with the access logged); **"insufficient evidence" as a first-class terminal outcome**, not an error; confidence thresholds that route to human review rather than guessing; and read-only session scoping for the agent so it provably cannot mutate source data.

### 6.7 Reusable skills

Three or four genuinely reusable, clearly documented skills. Candidates that would actually help another team:

- `policy-to-rule-extractor` — circular/policy PDF → structured effective-dated rules
- `evidence-bundle-builder` — case ID → re-verifiable evidence pack with point-in-time query refs
- `regulatory-narrative-drafter` — finding + evidence → cited narrative, with the numeric reconciliation gate built in
- `planted-typology-generator` — synthetic data with ground-truth labels

Note these are *generic* — none of them is "our demo, wrapped." That's the bar for a reusable skill.

### 6.8 Surfaces

CLI for the engineering/orchestration story, Streamlit for the compliance user, Slack for alert-to-approval, Snowsight for the cloud-agent angle. Same semantic layer and same findings tables behind all of them — that's the point you want to land: one governed core, many surfaces.

---

## 7. Scope: go deep on two, thread all four

Four risk domains built shallowly will read as thin. My recommendation:

**Deep:** Fraud + AML. They share the transaction spine and the network/graph angle, so the marginal cost of the second is low, and AML gives you the best artifact (STR).

**One prudential metric, done properly:** LCR *or* CRAR — not all of Basel. You need exactly one to prove the "balance-sheet regulatory output" axis. Pick whichever your synthetic balance sheet supports most cleanly.

**Credit risk as the bridge:** SMA/DPD staging is cheap to compute and it's the connective tissue — it feeds both provisioning and the capital metric.

Then the demo:

> A single synthetic counterparty. Fraud signals fire on pass-through velocity and a shared-device link to three other accounts. The investigation escalates to an AML case; the ring is traced with a graph query; the agent drafts a cited STR and the reviewer agent validates every number before a human attests. The same counterparty is a borrower — the fraud finding triggers early-warning review, credit staging moves, provisioning changes, and the exposure flows through to the CRAR computation, which now sits closer to its floor. The agent explains the chain in one narrative, citing the transactions, the policy clauses, and the as-of timestamps.

**One entity, one story, four pillars, one evidence chain.** That is a fundamentally better demo than four disconnected features, and it's the argument for why joining these silos matters — which is the real-world relevance score.

---

## 8. What to actually go read

**Regulatory primary sources** (verify current versions — these get revised):
- RBI Master Directions for NBFCs under the **Scale-Based Regulation** framework — layer definitions, CRAR and Tier 1 minimums, applicability thresholds
- RBI **Master Direction on Fraud Risk Management** (consolidated, revised 2024) — classification, reporting timelines, governance
- RBI **IRAC norms** — DPD, SMA-0/1/2, NPA ageing
- RBI **ALM guidelines** — maturity buckets and cumulative gap tolerances
- **PMLA + PML Rules**, and **FIU-IND** reporting formats — CTR/STR/CCR/NTR/CBWTR structure and fields
- **BCBS Basel III** framework and **Pillar 3 disclosure templates** — for structure and vocabulary
- **Ind AS 109** — ECL staging criteria

**Typology and control material:**
- **FATF** 40 Recommendations plus their typology reports — the canonical vocabulary for money-laundering patterns
- **Wolfsberg Group** statements and questionnaires — excellent source of precise control language you can lift into your policy model
- **ACAMS** typology material — practitioner-level red flag catalogues

**Snowflake capability research** (via `cortex search docs`, and the relevant skills):
- dynamic tables (incremental refresh, target lag), streams, tasks, Snowpipe Streaming
- Cortex Analyst semantic views + verified queries
- `AI_PARSE_DOCUMENT`, `AI_EXTRACT`, `AI_CLASSIFY` for the document lane
- Time Travel for point-in-time reproduction
- masking policies, row access policies, tags/classification for the governance story
- recursive CTE patterns and their limits, for graph traversal

---

## 9. Failure modes to design against

- **Chat-over-tables masquerading as a copilot.** No evidence chain, no artifact, no sign-off.
- **The LLM computing regulatory figures.** Fatal with any judge who knows the domain.
- **No as-of discipline** — numbers shift between demo runs and you can't explain why.
- **Policy RAG that returns paragraphs instead of clause-level, effective-dated rules.**
- **Skipping maker-checker.** An agent that files reports autonomously is, to a compliance audience, a compliance *incident*.
- **No measured accuracy.** "It works" without a confusion matrix is unverifiable.
- **Vanity multi-agent.** Five agents passing text around scores worse than three with real, distinct responsibilities and clean handoffs.
- **Perfect-path-only demo.** Show the refusal. Show "insufficient evidence." Show a false positive being correctly closed with a documented rationale. Graceful failure is on their scoring list explicitly and is the easiest place to stand out, because nobody rehearses it.

---

## 10. Sequencing, and your CoCo evidence trail

They want CoCo visible in all four phases, so generate the evidence as a by-product rather than reconstructing it later. Concretely: keep the planning session transcripts, the ontology draft, the semantic-view validation output, the reconciliation test runs, the precision/recall report, and the automation definitions. `cortex conversations search` and saved transcripts are legitimate artefacts here.

Order I'd work in:

1. **Ontology + evidence-chain schema.** On paper, in CoCo, before any data. Everything downstream depends on `Evidence` being pointers.
2. **Synthetic data with planted ground truth + reconciliation tests.** The tests are a gate, not an afterthought.
3. **Semantic views + verified queries for every regulatory metric.** Validate against natural-language questions now, not at the end.
4. **Detection layer** — rules and graph queries, scored against ground truth so you have numbers from day one.
5. **Document lane** — policy/circular extraction into effective-dated clauses.
6. **Agent chain** — signal → investigate → policy-map → draft → **review**. Build the reviewer early; it changes what the drafter needs to produce.
7. **Artifact generation + numeric reconciliation gate + attestation workflow.**
8. **Surfaces and MCP**, last. They're the shallowest layer and the most tempting to start with.

The discipline to resist: don't build the Streamlit app first. It's the most visible layer and the least load-bearing, and time spent there early is time not spent on the evidence chain that actually differentiates you.

---

Two things I'd suggest as immediate next steps, if you want me to take them on: I can run the Snowflake-side capability research properly (what's currently supported for the graph traversal, the document extraction functions, and point-in-time patterns) so your architecture is grounded in what actually works rather than what should; or I can draft the full ontology and evidence-chain DDL so you've got the foundation from §4 concrete and reviewable. The ontology is the higher-leverage one — everything else keys off it.


---

## Turn 4 — User

How to save these convos for review later? As markdowns or are there skills/snowflakes features for the same?

### Assistant

Let me pull the authoritative reference on this rather than guess at flags.

### Assistant

Found this session: `b48f8e68-7502-4b92-a394-aef83980001a`. Let me check the transcript structure so I can show you a clean export.

### Assistant

JSONL, one object per line. Let me build you a reusable exporter rather than a one-off command.

### Assistant

The backtick-heavy string interpolation broke the parser. Let me check whether Python's available — it'll be much cleaner for JSON handling.
