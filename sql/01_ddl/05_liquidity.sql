-- ===========================================================================
-- 01_ddl / 05_liquidity.sql
-- ===========================================================================
-- The liquidity slice: the RBI Statement of Structural Liquidity for an NBFC,
-- on the same governed spine as the AML chain.
--
-- ALM_CASHFLOW holds an ILLUSTRATIVE ALM book of the fictional NBFC
-- (conf/generation.yml:alm_book), bucketed by residual maturity. It is not
-- derived from the AMLworld ledger. Reference data: upserted from config.
--
-- V_STRUCTURAL_LIQUIDITY computes, per bucket, the net and cumulative mismatch
-- and the cumulative negative mismatch as a percentage of cumulative outflows.
-- Each limited bucket names a tolerance clause; the limit itself is read from
-- V_POLICY_CLAUSE_CURRENT, so changing it is a governed clause version, not an
-- edit here. Every figure is SQL; nothing is estimated by a model.
-- ===========================================================================

USE SCHEMA {{ fq.curated }};

CREATE TABLE IF NOT EXISTS ALM_CASHFLOW (
    as_of_date          DATE          NOT NULL,
    line_item           VARCHAR       NOT NULL,
    side                VARCHAR       NOT NULL COMMENT 'INFLOW | OUTFLOW.',
    bucket_id           NUMBER(10,0)  NOT NULL COMMENT 'Residual-maturity bucket, 1 = shortest.',
    bucket_label        VARCHAR       NOT NULL,
    from_day            NUMBER(10,0)  NOT NULL,
    to_day              NUMBER(10,0)  COMMENT 'NULL = open-ended (over 5 years).',
    tolerance_clause_id VARCHAR       COMMENT 'POLICY_CLAUSE id holding this bucket''s limit; NULL = no regulatory limit.',
    amount_inr          NUMBER(20,2)  NOT NULL,
    CONSTRAINT pk_alm_cashflow PRIMARY KEY (as_of_date, line_item, bucket_id)
)
COMMENT = 'Illustrative ALM book of the fictional NBFC, by maturity bucket. Seeded from conf/generation.yml:alm_book.';

{% if alm_book %}
MERGE INTO ALM_CASHFLOW t
USING (
{% for row in alm_book %}
    SELECT {% for column, literal in row.items() %}{{ literal }} AS {{ column }}{{ ", " if not loop.last else "" }}{% endfor %}{{ "\n    UNION ALL" if not loop.last else "" }}
{% endfor %}
) s
   ON t.as_of_date = s.as_of_date AND t.line_item = s.line_item AND t.bucket_id = s.bucket_id
WHEN MATCHED THEN UPDATE SET
    side = s.side,
    bucket_label = s.bucket_label,
    from_day = s.from_day,
    to_day = s.to_day,
    tolerance_clause_id = s.tolerance_clause_id,
    amount_inr = s.amount_inr
WHEN NOT MATCHED THEN INSERT (
    as_of_date, line_item, side, bucket_id, bucket_label, from_day, to_day, tolerance_clause_id, amount_inr
) VALUES (
    s.as_of_date, s.line_item, s.side, s.bucket_id, s.bucket_label, s.from_day, s.to_day,
    s.tolerance_clause_id, s.amount_inr
);
{% endif %}

CREATE OR REPLACE VIEW {{ view.structural_liquidity }}
    COMMENT = 'Statement of Structural Liquidity: cumulative mismatch per bucket against the POLICY_CLAUSE tolerance limits.'
AS
WITH buckets AS (
    SELECT as_of_date, bucket_id, bucket_label, from_day, to_day, tolerance_clause_id,
           SUM(IFF(side = 'INFLOW', amount_inr, 0))  AS inflow_inr,
           SUM(IFF(side = 'OUTFLOW', amount_inr, 0)) AS outflow_inr
    FROM ALM_CASHFLOW
    GROUP BY ALL
), running AS (
    SELECT *,
           inflow_inr - outflow_inr AS mismatch_inr,
           SUM(inflow_inr - outflow_inr) OVER (
               PARTITION BY as_of_date ORDER BY bucket_id ROWS UNBOUNDED PRECEDING
           ) AS cumulative_mismatch_inr,
           SUM(outflow_inr) OVER (
               PARTITION BY as_of_date ORDER BY bucket_id ROWS UNBOUNDED PRECEDING
           ) AS cumulative_outflow_inr
    FROM buckets
), scored AS (
    SELECT r.*,
           100 * r.cumulative_mismatch_inr / NULLIF(r.cumulative_outflow_inr, 0) AS cumulative_mismatch_pct,
           p.clause_id      AS tolerance_clause,
           p.threshold_lower AS tolerance_pct,
           p.source_document AS tolerance_source,
           p.action_required AS tolerance_action
    FROM running r
    LEFT JOIN {{ fq.governance }}.V_POLICY_CLAUSE_CURRENT p ON p.clause_id = r.tolerance_clause_id
)
SELECT as_of_date, bucket_id, bucket_label, from_day, to_day, tolerance_clause_id,
       inflow_inr, outflow_inr, mismatch_inr, cumulative_mismatch_inr, cumulative_outflow_inr,
       cumulative_mismatch_pct, tolerance_clause, tolerance_pct, tolerance_source, tolerance_action,
       CASE
           WHEN tolerance_clause_id IS NULL THEN 'NO_LIMIT'
           -- Named but not in force: the policy stage has not seeded it.
           WHEN tolerance_clause IS NULL THEN 'LIMIT_MISSING'
           WHEN cumulative_mismatch_pct >= 0 THEN 'WITHIN'
           WHEN -cumulative_mismatch_pct <= tolerance_pct THEN 'WITHIN'
           ELSE 'BREACH'
       END AS status
FROM scored;
