-- ===========================================================================
-- verify / 04_loaded_seed.sql -- admin only
-- ===========================================================================
-- The generation seed(s) of the loaded injected legs. The hero pin names a
-- seed; confirming it against data built from another seed would certify the
-- wrong party.
-- ===========================================================================
SELECT
    ARRAY_AGG(DISTINCT generation_seed) WITHIN GROUP (ORDER BY generation_seed) AS seeds,
    COUNT(*) AS legs
FROM {{ fq.ground_truth }}.INJECTED_TXN_PENDING;
