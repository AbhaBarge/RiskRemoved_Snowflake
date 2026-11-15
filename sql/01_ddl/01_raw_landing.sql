-- ===========================================================================
-- 01_ddl / 01_raw_landing.sql
-- ===========================================================================
-- Immutable landing tables. Append-only: nothing here is ever updated in place,
-- which is what makes Time Travel re-verification meaningful.
--
-- GAP-03 RESOLVED.  The architecture doc defined RAW_TRANSACTIONS twice --
-- once pair-level and AMLworld-shaped (from_bank, amount_paid, is_laundering)
-- and once leg-level with signed INR amounts. Both cannot hold. The split is
-- explicit here:
--
--   RAW_AMLWORLD_TRANS   verbatim source, pair-level, label included
--        |  reskin (FX -> INR, channel remap, pair -> two legs)
--        +-> RAW_TRANSACTIONS            leg-level, signed INR, NO label
--        +-> GROUND_TRUTH.TXN_LABELS     labels, isolated schema
--
-- Keeping the label out of RAW_TRANSACTIONS is not tidiness: RAW_TRANSACTIONS
-- feeds the dynamic tables that feed the semantic view, so a label column here
-- would leak the answer to the agent.
-- ===========================================================================

USE SCHEMA {{ fq.raw }};

-- --------------------------------------------------------------------------
-- Verbatim AMLworld landing (pair-level).
--
-- Loaded positionally ($1..$11) because columns 3 and 5 of HI-Small_Trans.csv
-- share the header "Account"; a header-based load either errors or silently
-- drops one of them.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_AMLWORLD_TRANS (
    src_row_number      NUMBER(38,0)    NOT NULL COMMENT '1-based data-row ordinal: METADATA$FILE_ROW_NUMBER under SKIP_HEADER = 1, which does not count the header (M16). Joins GROUND_TRUTH.TXN_LABELS.src_row_number.',
    txn_timestamp       TIMESTAMP_NTZ   NOT NULL COMMENT 'Source format YYYY/MM/DD HH:MI.',
    from_bank           VARCHAR         NOT NULL,
    from_account        VARCHAR         NOT NULL COMMENT 'CSV column 3 (hex). Unique only within from_bank.',
    to_bank             VARCHAR         NOT NULL,
    to_account          VARCHAR         NOT NULL COMMENT 'CSV column 5 (hex). Unique only within to_bank.',
    amount_received     NUMBER(20,6)    NOT NULL COMMENT 'Credited to the beneficiary, in receiving_currency. Scale 6: Bitcoin carries 6 dp, every other currency 2 (M14); scale 2 would round a verbatim table.',
    receiving_currency  VARCHAR         NOT NULL,
    amount_paid         NUMBER(20,6)    NOT NULL COMMENT 'Debited from the originator, in payment_currency. Scale 6, as amount_received.',
    payment_currency    VARCHAR         NOT NULL,
    payment_format      VARCHAR         NOT NULL COMMENT 'ACH | Wire | Cheque | Cash | Credit Card | Reinvestment.',
    is_laundering       NUMBER(1,0)     NOT NULL COMMENT 'Source ground truth. Routed to GROUND_TRUTH; never propagated to CURATED.',
    loaded_at           TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP() NOT NULL
)
COMMENT = 'IBM AMLworld HI-Small transactions, unmodified. Pair-level grain. CDLA-Sharing-1.0.';

CREATE TABLE IF NOT EXISTS RAW_AMLWORLD_ACCOUNTS (
    bank_name       VARCHAR NOT NULL,
    bank_id         VARCHAR NOT NULL,
    account_number  VARCHAR NOT NULL COMMENT 'Hex. Unique only within bank_id -- see ids.account_id().',
    entity_id       VARCHAR NOT NULL COMMENT 'Hex owning entity. The referential backbone (GAP-02).',
    entity_name     VARCHAR NOT NULL COMMENT 'e.g. "Corporation #33520". party_type is derived from the prefix.',
    loaded_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL
)
COMMENT = 'AMLworld account-to-entity mapping. 518,581 rows. Source of the account/party skeleton.';

-- --------------------------------------------------------------------------
-- GAP-02: the identity maps.
--
-- These are the single most load-bearing tables in the build. The original plan
-- generated Faker parties with fresh ids while transactions referenced AMLworld
-- hex accounts, so every downstream join would have returned nothing. Instead,
-- AMLworld keys are authoritative and the surrogate id is a deterministic
-- function of the source key (risk_copilot.ids). Referential integrity then
-- holds by construction rather than by convention.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ID_MAP_PARTY (
    party_id        VARCHAR NOT NULL COMMENT 'Surrogate P-####### from ids.party_id(entity_id, seed).',
    src_entity_id   VARCHAR NOT NULL COMMENT 'AMLworld entity_id (hex).',
    generation_seed NUMBER(38,0) NOT NULL COMMENT 'Seed that produced this mapping. Reproducibility anchor.',
    in_subgraph     BOOLEAN NOT NULL DEFAULT TRUE COMMENT 'Survived subgraph sampling.',
    loaded_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_id_map_party PRIMARY KEY (party_id),
    CONSTRAINT uk_id_map_party_src UNIQUE (src_entity_id)
)
COMMENT = 'Deterministic AMLworld entity_id -> party_id map. Asserted 1:1 at generation time.';

CREATE TABLE IF NOT EXISTS ID_MAP_ACCOUNT (
    account_id          VARCHAR NOT NULL COMMENT 'Surrogate A-######## from ids.account_id(bank_id, account_number, seed).',
    src_bank_id         VARCHAR NOT NULL,
    src_account_number  VARCHAR NOT NULL,
    party_id            VARCHAR NOT NULL COMMENT 'Owning party. FK to ID_MAP_PARTY.',
    generation_seed     NUMBER(38,0) NOT NULL,
    in_subgraph         BOOLEAN NOT NULL DEFAULT TRUE,
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_id_map_account PRIMARY KEY (account_id),
    CONSTRAINT uk_id_map_account_src UNIQUE (src_bank_id, src_account_number),
    CONSTRAINT fk_id_map_account_party FOREIGN KEY (party_id) REFERENCES ID_MAP_PARTY (party_id)
)
COMMENT = 'Deterministic (bank_id, account_number) -> account_id map. Composite key is required: AMLworld account numbers repeat across banks, and deriving from the number alone would merge distinct accounts and corrupt the counterparty graph.';

-- --------------------------------------------------------------------------
-- Leg-level transactions -- the table the pipeline actually reads.
--
-- One AMLworld pair row becomes two rows sharing txn_pair_id: a signed-negative
-- debit and a signed-positive credit. Signed amounts make inflow and outflow a
-- filter rather than a column, which keeps the velocity SQL simple.
--
-- Deliberately NOT present: is_laundering. See the header note.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_TRANSACTIONS (
    txn_id                  VARCHAR         NOT NULL COMMENT 'T-############-D|C.',
    txn_pair_id             VARCHAR         NOT NULL COMMENT 'Links the two legs of one transfer (GAP-03).',
    leg_side                VARCHAR(1)      NOT NULL COMMENT 'D = debit (negative), C = credit (positive).',
    account_id              VARCHAR         NOT NULL COMMENT 'The account this leg posts to.',
    counterparty_account_id VARCHAR         NOT NULL COMMENT 'The other side. Resolvable by construction.',
    txn_timestamp           TIMESTAMP_NTZ   NOT NULL,
    amount                  NUMBER(20,2)    NOT NULL COMMENT 'Signed, in `currency`. Negative = debit.',
    currency                VARCHAR         NOT NULL COMMENT 'INR for ~95% of legs; the rest cross-border.',
    amount_inr              NUMBER(20,2)    NOT NULL COMMENT 'Converted at the per-currency rate in conf/generation.yml. All threshold arithmetic uses this column.',
    txn_type                VARCHAR         NOT NULL COMMENT 'DEPOSIT | WITHDRAWAL | TRANSFER | PAYMENT.',
    channel                 VARCHAR         NOT NULL COMMENT 'UPI | IMPS | NEFT | RTGS | CASH | CHEQUE.',
    reference_text          VARCHAR         COMMENT 'Free-text narration. Synthetic.',
    source_ref              VARCHAR         COMMENT 'Opaque UTR-like transfer reference, shared by both legs. Deliberately carries no provenance: a source/injected prefix or a source row number would be a label. Provenance is in GROUND_TRUTH.TXN_LABELS.',
    ingested_at             TIMESTAMP_NTZ   DEFAULT CURRENT_TIMESTAMP() NOT NULL
                                            COMMENT 'Arrival time. Differs from txn_timestamp under replay.',
    held_back               BOOLEAN         DEFAULT FALSE NOT NULL
                                            COMMENT 'Live simulator: TRUE = loaded but not yet arrived. Detection (DT_TRANSACTIONS_ENRICHED) reads only FALSE rows. Not a label: it is set by booking time alone.',
    released_at             TIMESTAMP_NTZ   COMMENT 'When SIM_CONTROL released a held-back leg; NULL for a leg that was never held. The app shows COALESCE(released_at, ingested_at) as arrival.',
    CONSTRAINT pk_raw_transactions PRIMARY KEY (txn_id),
    CONSTRAINT fk_raw_txn_account FOREIGN KEY (account_id) REFERENCES ID_MAP_ACCOUNT (account_id)
)
CLUSTER BY (TO_DATE(txn_timestamp), account_id)
COMMENT = 'Leg-level transaction ledger, INR-normalised. Append-only. Contains NO label column: it feeds the dynamic tables that feed the semantic view.';
-- held_back / released_at arrive with CREATE. No ADD COLUMN IF NOT EXISTS for a
-- ledger created earlier: with a DEFAULT, Snowflake rejects it once the column
-- exists ("ambiguous column name"), so it would break every re-deploy.

-- --------------------------------------------------------------------------
-- Party attributes, split across two sources so the agent has to reconcile them.
--
-- A real institution holds the same customer in a core banking system and a KYC
-- platform, and they disagree. Modelling that disagreement is the point: the
-- L3 generator injects contradictions at a configured rate, and DT_DIM_PARTY
-- has to resolve them.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_PARTY_CBS (
    party_id            VARCHAR NOT NULL,
    full_name           VARCHAR NOT NULL,
    party_type          VARCHAR NOT NULL COMMENT 'INDIVIDUAL | LEGAL_PERSON.',
    date_of_birth       DATE            COMMENT 'NULL for legal persons.',
    incorporation_date  DATE            COMMENT 'NULL for individuals.',
    nationality         VARCHAR,
    branch_region       VARCHAR NOT NULL COMMENT 'Row access policy discriminator.',
    branch_code         VARCHAR NOT NULL,
    customer_since      DATE,
    status              VARCHAR NOT NULL COMMENT 'ACTIVE | DORMANT | CLOSED.',
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_party_cbs PRIMARY KEY (party_id)
)
COMMENT = 'Core banking system view of the customer. System of record for identity and branch.';

CREATE TABLE IF NOT EXISTS RAW_PARTY_KYC (
    party_id                VARCHAR NOT NULL,
    occupation              VARCHAR         COMMENT 'Deliberately stale for a configured share of parties.',
    declared_annual_income  NUMBER(20,2)    COMMENT 'GAP-04: derived from observed legitimate inflow, not drawn at random, or INCOME_ACTIVITY_MISMATCH fires on everyone or no one.',
    source_of_funds         VARCHAR,
    risk_rating             VARCHAR         COMMENT 'LOW | MEDIUM | HIGH.',
    pep_flag                BOOLEAN NOT NULL DEFAULT FALSE,
    kyc_status              VARCHAR NOT NULL COMMENT 'VERIFIED | PENDING_REVIEW | EXPIRED.',
    kyc_last_reviewed       DATE,
    pan_number              VARCHAR         COMMENT 'Synthetic. Masked for non-privileged roles.',
    aadhaar_last4           VARCHAR         COMMENT 'Synthetic. Masked for non-privileged roles.',
    phone_number            VARCHAR         COMMENT 'Shared across parties by design -> SHARED_PHONE edges.',
    kyc_note                VARCHAR         COMMENT 'L3 narrative. May contradict the structured record.',
    loaded_at               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_party_kyc PRIMARY KEY (party_id),
    CONSTRAINT fk_raw_party_kyc_party FOREIGN KEY (party_id) REFERENCES RAW_PARTY_CBS (party_id)
)
COMMENT = 'KYC/CDD platform view. Source for income, PEP status and narrative notes.';

CREATE TABLE IF NOT EXISTS RAW_ACCOUNTS (
    account_id          VARCHAR NOT NULL,
    party_id            VARCHAR NOT NULL,
    account_type        VARCHAR NOT NULL COMMENT 'SAVINGS | CURRENT | LOAN | CREDIT_CARD | FD.',
    opened_date         DATE    NOT NULL,
    status              VARCHAR NOT NULL COMMENT 'ACTIVE | DORMANT | FROZEN | CLOSED.',
    current_balance     NUMBER(20,2),
    credit_limit        NUMBER(20,2)    COMMENT 'NULL unless a credit product.',
    branch_region       VARCHAR NOT NULL COMMENT 'Denormalised from the party for row-level filtering.',
    branch_code         VARCHAR NOT NULL,
    device_fingerprint  VARCHAR         COMMENT 'GAP-05: shared fingerprints are co-located with existing fund flows. Random assignment yields zero SHARED_DEVICE_CLUSTER hits, because that rule requires a shared device AND a flow between the accounts.',
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_accounts PRIMARY KEY (account_id),
    CONSTRAINT fk_raw_accounts_party FOREIGN KEY (party_id) REFERENCES RAW_PARTY_CBS (party_id)
)
COMMENT = 'Account master, enriched onto the preserved AMLworld account keys.';

-- --------------------------------------------------------------------------
-- Generated relationship edges.
--
-- Attribute-based only (shared phone, shared device, beneficial ownership).
-- FUND_FLOW edges are DERIVED from transactions in DT_PARTY_RELATIONSHIP and
-- must never be generated here: a generated flow edge that no transaction
-- supports would make graph evidence unverifiable.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_PARTY_RELATIONSHIP_ATTR (
    relationship_id     VARCHAR NOT NULL,
    party_a_id          VARCHAR NOT NULL,
    party_b_id          VARCHAR NOT NULL,
    relationship_type   VARCHAR NOT NULL COMMENT 'SHARED_PHONE | SHARED_DEVICE | BENEFICIAL_OWNER.',
    shared_value        VARCHAR         COMMENT 'The phone or fingerprint that links them. Evidence, not decoration.',
    first_seen          TIMESTAMP_NTZ,
    last_seen           TIMESTAMP_NTZ,
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_party_rel_attr PRIMARY KEY (relationship_id)
)
COMMENT = 'Attribute-based relationship edges. FUND_FLOW is derived from transactions, not generated.';

-- --------------------------------------------------------------------------
-- Sanctions landing.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_SANCTIONS_OPENSANCTIONS (
    entry_id            VARCHAR NOT NULL COMMENT 'OpenSanctions entity id.',
    schema_type         VARCHAR         COMMENT 'Person | Company | Organization | LegalEntity.',
    entity_name         VARCHAR NOT NULL,
    aliases_raw         VARCHAR         COMMENT 'Semicolon-delimited in source; split into an ARRAY downstream.',
    birth_date_raw      VARCHAR         COMMENT 'Partial dates occur ("1975", "1975-03"); kept as text, parsed later.',
    countries_raw       VARCHAR,
    datasets_raw        VARCHAR         COMMENT 'Contributing source lists (OFAC, UN, EU, UK).',
    first_seen          VARCHAR,
    last_seen           VARCHAR,
    last_change         VARCHAR         COMMENT 'Drives SANCTIONS_MATCH_NEW via the watermark table.',
    list_version        VARCHAR         COMMENT 'Snapshot version. Required provenance for a screening hit.',
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_sanctions_os PRIMARY KEY (entry_id)
)
COMMENT = 'OpenSanctions targets.simple.csv. CC-BY-NC-4.0: attribution required, non-commercial use.';

CREATE TABLE IF NOT EXISTS RAW_SANCTIONS_OFAC (
    entry_id        VARCHAR NOT NULL,
    entity_name     VARCHAR NOT NULL,
    aliases_raw     VARCHAR,
    sdn_type        VARCHAR,
    programs_raw    VARCHAR COMMENT 'Sanctions programmes the entity is designated under.',
    nationality     VARCHAR,
    listed_date     DATE,
    list_version    VARCHAR,
    loaded_at       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_sanctions_ofac PRIMARY KEY (entry_id)
)
COMMENT = 'OFAC SDN. Empty in the Tier-1 build; the live fetch is behind an EAI and disabled so the demo has no network dependency.';

-- --------------------------------------------------------------------------
-- GAP-11: sanctions screening watermark.
--
-- SANCTIONS_MATCH_NEW means "matches an entry added since the last check",
-- which is not expressible without persisted state. Without this table the rule
-- either re-fires on every refresh or never fires at all -- the architecture
-- specified the rule but not the mechanism.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS SANCTIONS_SCREENING_WATERMARK (
    watermark_id            VARCHAR NOT NULL DEFAULT 'SINGLETON',
    last_screened_at        TIMESTAMP_NTZ NOT NULL,
    last_list_version       VARCHAR,
    entries_at_last_screen  NUMBER(38,0),
    updated_at              TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_sanctions_watermark PRIMARY KEY (watermark_id)
)
COMMENT = 'Single-row watermark making "newly listed" well defined and idempotent.';

-- --------------------------------------------------------------------------
-- Policy clause staging. Extraction lands here and is NOT active until a human
-- approves it (Diagram 10). An auto-activated regulatory change is exactly the
-- failure mode the design refuses.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW_POLICY_CLAUSES (
    staging_id          VARCHAR NOT NULL,
    source_document     VARCHAR NOT NULL,
    source_page         NUMBER(10,0),
    section_heading     VARCHAR,
    clause_text         VARCHAR NOT NULL,
    extracted_metric    VARCHAR,
    extracted_operator  VARCHAR,
    extracted_lower     NUMBER(20,4),
    extracted_upper     NUMBER(20,4),
    extracted_effective DATE,
    action_required     VARCHAR,
    extraction_model    VARCHAR COMMENT 'Model that performed the extraction. Auditability of the extraction itself.',
    extraction_confidence NUMBER(5,4),
    raw_extraction      VARIANT COMMENT 'Full AI_EXTRACT response, retained for dispute resolution.',
    loaded_at           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP() NOT NULL,
    CONSTRAINT pk_raw_policy_clauses PRIMARY KEY (staging_id)
)
COMMENT = 'AI_PARSE_DOCUMENT + AI_EXTRACT output. Inert until promoted through POLICY_CHANGE_PROPOSALS by a human reviewer.';
