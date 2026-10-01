-- ===========================================================================
-- 07_semantic / 01_semantic_view.sql
-- ===========================================================================
-- SV_RISK_COPILOT: the business model Cortex Analyst answers plain-English
-- questions from ("which branch had the most cash deposits?", "how many open
-- cases are high severity?").
--
-- What is deliberately NOT here:
--   * GROUND_TRUTH -- the labels. Nothing an analyst or a model can query may
--     reach the answer key (the label-leakage gate's rule).
--   * PAN, Aadhaar, phone, KYC free text -- masking policies need Enterprise;
--     until then the cheapest control is not to expose them at all.
--   * Counterparty/desk internals -- available in CURATED for detection, not
--     needed to answer business questions.
-- ===========================================================================
{% set ENR = fq.curated ~ "." ~ dt.transactions_enriched.name %}
{% set PARTY = fq.curated ~ "." ~ dt.dim_party.name %}

CREATE OR REPLACE SEMANTIC VIEW {{ fq.curated }}.{{ cortex.semantic_view }}
    TABLES (
        transactions AS {{ ENR }}
            PRIMARY KEY (txn_id)
            WITH SYNONYMS = ('payments', 'deposits', 'ledger')
            COMMENT = 'One row per transaction leg, in INR. A transfer has a debit (D) leg and a credit (C) leg.',
        customers AS {{ PARTY }}
            PRIMARY KEY (party_id)
            WITH SYNONYMS = ('parties', 'clients', 'account holders')
            COMMENT = 'Bank customers with their KYC profile.',
        cases AS {{ fq.evidence }}.CASE_RECORD
            PRIMARY KEY (case_id)
            WITH SYNONYMS = ('investigations', 'alerts')
            COMMENT = 'AML investigation cases, one per customer under investigation.',
        signals AS {{ fq.evidence }}.SIGNAL
            PRIMARY KEY (signal_id)
            WITH SYNONYMS = ('rule hits', 'detections')
            COMMENT = 'Detection rule firings that opened or support a case.',
        findings AS {{ fq.evidence }}.FINDING
            PRIMARY KEY (finding_id)
            COMMENT = 'Versioned investigation conclusions behind each STR.',
        strs AS {{ fq.evidence }}.ARTIFACT
            PRIMARY KEY (artifact_id)
            WITH SYNONYMS = ('suspicious transaction reports', 'filings', 'reports')
            COMMENT = 'Suspicious Transaction Report drafts and filings.'
    )
    RELATIONSHIPS (
        transaction_customer AS transactions (party_id) REFERENCES customers,
        case_customer AS cases (party_id) REFERENCES customers,
        signal_customer AS signals (party_id) REFERENCES customers,
        finding_case AS findings (case_id) REFERENCES cases,
        str_finding AS strs (finding_id) REFERENCES findings
    )
    FACTS (
        transactions.amount_inr AS amount_inr
            COMMENT = 'Leg amount in Indian rupees.',
        customers.declared_annual_income AS declared_annual_income
            COMMENT = 'Annual income the customer declared at KYC, in INR.',
        signals.metric_value AS metric_value
            COMMENT = 'The value the rule measured, e.g. distinct accounts used.'
    )
    DIMENSIONS (
        transactions.txn_id AS txn_id,
        transactions.txn_pair_id AS txn_pair_id
            COMMENT = 'Identifies one transaction; its debit and credit legs share it.',
        transactions.txn_date AS txn_date
            WITH SYNONYMS = ('date', 'transaction date'),
        transactions.txn_timestamp AS txn_timestamp,
        transactions.leg_side AS leg_side
            COMMENT = 'C = credit (money into the account), D = debit (money out).',
        transactions.channel AS channel
            WITH SYNONYMS = ('payment mode', 'payment channel')
            COMMENT = 'CASH, UPI, IMPS, NEFT, RTGS or CHEQUE.',
        transactions.txn_type AS txn_type
            COMMENT = 'DEPOSIT, PAYMENT or TRANSFER.',
        transactions.is_cash AS is_cash,
        transactions.account_id AS account_id,
        transactions.txn_branch_region AS branch_region
            WITH SYNONYMS = ('branch', 'region')
            COMMENT = 'Branch region of the account holder: MUMBAI, DELHI or PUNE.',
        customers.party_id AS party_id
            WITH SYNONYMS = ('customer id'),
        customers.customer_name AS full_name
            WITH SYNONYMS = ('name', 'customer'),
        customers.party_type AS party_type
            COMMENT = 'INDIVIDUAL or a business type.',
        customers.occupation AS occupation,
        customers.risk_rating AS risk_rating
            COMMENT = 'KYC risk rating: LOW, MEDIUM or HIGH.',
        customers.pep_flag AS pep_flag
            WITH SYNONYMS = ('politically exposed person'),
        customers.kyc_status AS kyc_status,
        customers.customer_branch_region AS branch_region,
        customers.customer_since AS customer_since,
        cases.case_id AS case_id,
        cases.case_status AS status
            COMMENT = 'OPEN, INVESTIGATING, DRAFTED, REVIEWED or CLOSED.',
        cases.case_type AS case_type,
        cases.case_branch_region AS branch_region,
        cases.opened_at AS opened_at,
        cases.sla_deadline AS sla_deadline
            WITH SYNONYMS = ('filing deadline'),
        signals.signal_id AS signal_id,
        signals.rule_id AS rule_id
            WITH SYNONYMS = ('rule', 'detection rule'),
        signals.severity AS severity,
        signals.window_bucket AS window_bucket,
        strs.artifact_id AS artifact_id,
        strs.str_status AS status
            COMMENT = 'DRAFT, REJECTED, REVIEWED, FILED, RETURNED or SUPERSEDED.',
        strs.str_created_at AS created_at
    )
    METRICS (
        transactions.total_amount_inr AS SUM(transactions.amount_inr)
            COMMENT = 'Sum of leg amounts in INR. Filter leg_side = C for money received.',
        transactions.transaction_count AS COUNT(DISTINCT transactions.txn_pair_id)
            COMMENT = 'Number of distinct transactions.',
        transactions.cash_deposit_amount_inr AS SUM(IFF(transactions.is_cash AND transactions.leg_side = 'C', transactions.amount_inr, 0))
            WITH SYNONYMS = ('cash deposited', 'cash deposits value')
            COMMENT = 'Total cash deposited into customer accounts, INR.',
        transactions.cash_deposit_count AS COUNT_IF(transactions.is_cash AND transactions.leg_side = 'C')
            WITH SYNONYMS = ('number of cash deposits'),
        customers.customer_count AS COUNT(customers.party_id),
        cases.case_count AS COUNT(cases.case_id)
            WITH SYNONYMS = ('number of cases'),
        signals.signal_count AS COUNT(signals.signal_id),
        strs.str_count AS COUNT(strs.artifact_id),
        strs.filed_str_count AS COUNT_IF(strs.str_status = 'FILED')
            WITH SYNONYMS = ('STRs filed', 'filed reports')
    )
    COMMENT = 'Risk & Compliance Copilot: transactions, customers, cases, signals and STRs. No ground-truth labels.';
