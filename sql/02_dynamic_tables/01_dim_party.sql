-- ===========================================================================
-- 02_dynamic_tables / 01_dim_party.sql
-- ===========================================================================
-- One current-state row per customer: the core banking record joined to the
-- KYC record. Current state only -- no effective dating (architecture decision:
-- SCD2 is future scope).
--
-- Lag {{ dt.dim_party.target_lag }}: refreshed when DT_TRANSACTIONS_ENRICHED
-- needs it. CBS/KYC change rarely, so an incremental refresh is nearly free, and
-- a fixed long lag here would be rejected under a 1-minute consumer (P2).
--
-- The cash desk is not a customer and has no CBS row, so it never appears.
-- PAN, Aadhaar and phone are carried for the masking policies (governance
-- stage); nothing downstream of this table filters on them.
-- ===========================================================================
{% from "02_dynamic_tables/_macros.sql" import target_lag %}

CREATE OR ALTER DYNAMIC TABLE {{ fq.curated }}.{{ dt.dim_party.name }}
    TARGET_LAG = {{ target_lag(dt.dim_party) }}
    WAREHOUSE = {{ wh.pipeline }}
    REFRESH_MODE = INCREMENTAL
    COMMENT = 'Conformed customer: RAW_PARTY_CBS + RAW_PARTY_KYC, current state. No label.'
AS
SELECT
    c.party_id,
    c.full_name,
    c.party_type,
    c.date_of_birth,
    c.incorporation_date,
    c.nationality,
    c.branch_region,
    c.branch_code,
    c.customer_since,
    c.status,
    k.occupation,
    k.declared_annual_income,
    k.source_of_funds,
    k.risk_rating,
    k.pep_flag,
    k.kyc_status,
    k.kyc_last_reviewed,
    k.pan_number,
    k.aadhaar_last4,
    k.phone_number,
    k.kyc_note
FROM {{ fq.raw }}.RAW_PARTY_CBS c
LEFT JOIN {{ fq.raw }}.RAW_PARTY_KYC k
       ON k.party_id = c.party_id;
