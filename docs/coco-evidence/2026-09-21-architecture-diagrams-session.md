# Architecture Diagrams & Visual Design Session

**Session date:** 2026-09-21
**Purpose:** Convert the dense text-based data sources analysis into visual Mermaid diagrams for the entire Risk & Compliance Copilot architecture. Created after user feedback that the prior text-heavy document was too difficult to parse.

---

## Decision: Mermaid over FigJam

**Considered:** FigJam connector, Mermaid, draw.io, PlantUML
**Chosen:** Mermaid — renders natively in GitHub, VS Code, Streamlit, and most markdown viewers. No external tool, no export step, no account required. Least effort, best quality.

---

## Diagrams Created (12 total)

All in `docs/architecture-diagrams.md`:

1. **Entity Relationship Model** — 13 entities (Party, Account, Transaction, PartyRelationship, Exposure, Signal, Case, Evidence, Finding, PolicyClause, Artifact, Attestation, SanctionsEntry) with full field lists and relationship cardinalities.

2. **Data Ingestion Architecture** — Color-coded by method:
   - Blue: Snowpipe Streaming (CBS transactions, 5s latency)
   - Purple: Batch COPY INTO (KYC, LMS, GL — daily)
   - Green: HTTP GET via External Access Integration (sanctions lists — daily)
   - Orange: Marketplace zero-copy share (TRUVITY PEP, IPinfo, MaxMind)
   - Red: AI_PARSE_DOCUMENT + AI_EXTRACT (RBI circulars — on demand)

3. **Two-Speed Transformation Pipeline** — Fast lane (DT_TRANSACTIONS_ENRICHED at 1 min, DT_TXN_VELOCITY_FEATURES at 5 min, DT_SIGNALS_FAST at 5 min) and slow lane (DT_EXPOSURES_STAGED, DT_EXPOSURE_PORTFOLIO, DT_PROVISIONING all at 1 day), with shared dimensions (DT_DIM_PARTY, DT_PARTY_RELATIONSHIP, DT_SANCTIONS_CONSOLIDATED, POLICY_CLAUSE) feeding both.

4. **Signal-to-Artifact Pipeline** — The 7-stage evidence chain: Detect -> Group -> Gather -> Reason -> Draft -> Review -> Attest. Shows adversarial reviewer feedback loop (reject -> re-draft).

5. **Cross-Domain Signal Detection** — 7 signals the TMS cannot compute (INCOME_ACTIVITY_MISMATCH, SHARED_ATTRIBUTE_CLUSTER, MULE_RING_CANDIDATE, STRUCTURING_CROSS_ACCOUNT, PEP_TRANSACTION_ANOMALY, FRAUD_BORROWER_OVERLAP, SANCTIONS_MATCH_NEW), with data source inputs for each.

6. **Notification and Case Routing** — Stream on DT_SIGNALS_FAST -> ROUTE_SIGNALS task -> decision logic (existing case? severity threshold?) -> channel routing (Slack #fraud-ops, compliance queue, CRO dashboard, immediate alert for sanctions).

7. **Policy Ingestion Pipeline** — PDF -> AI_PARSE_DOCUMENT -> AI_EXTRACT -> staging -> diff -> review queue -> human approval -> POLICY_CLAUSE. Human-gated. Shows rejected path.

8. **Demo Spine** — One entity (Rajesh Kumar, P-10042) across four lenses:
   - Fraud: velocity signals, shared device cluster, mule ring candidate
   - AML: case opened, graph query traces ring, STR drafted, MLRO attests
   - Credit: Rs 15L exposure at 42 DPD, staging moves to Stage 2, provisioning changes
   - Capital: exposure flows to CRAR, RWA increases, capital closer to floor

9. **External Data Sources** — Public (UN, OFAC, EU, UK, MHA, FATF) vs Marketplace (TRUVITY, IPinfo, MaxMind, FactSet, OpenCorporates), ingestion method per source.

10. **Complete Storage Model** — All tables in 4 categories: Immutable Landing (append-only), Dynamic Tables (Snowflake-managed), Governed State (human-gated), Evidence Chain (append-only audit trail).

11. **Target Users** — Fraud Analyst, AML Compliance Officer, MLRO, Credit Risk Analyst, Treasury/ALM Officer mapped to copilot capabilities they use.

12. **System Boundary** — Existing systems kept (CBS, TMS, LMS, KYC) vs copilot's new capabilities (cross-silo bridge, evidence chain, policy-as-data, audit-ready outputs). Shows data flow direction: existing systems feed the copilot.

---

## Key Design Decisions Reflected

- Two-speed architecture is visually explicit (diagram 3): fraud signals at 5-minute lag, balance sheet metrics at 1-day lag, shared dimensions serving both
- Evidence-as-pointers (not copies) is called out in diagram 4 and the ER model
- Human-gated policy activation is a separate diagram (7) emphasizing the review queue and reject path
- The demo spine (diagram 8) shows how one entity creates the narrative thread across all four pillars
- System boundary diagram (12) makes explicit that the copilot complements, not competes with, existing banking systems

---

## File Location

`C:\Users\ASHWIN\Music\HackPrepSFCoCoCLI\RiskRemoved_Snowflake\docs\architecture-diagrams.md`

Renders in: GitHub markdown preview, VS Code (Markdown Preview Mermaid Support extension), Streamlit, mermaid.live
