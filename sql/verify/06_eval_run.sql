-- ===========================================================================
-- verify / 06_eval_run.sql -- admin only
-- ===========================================================================
-- One EVAL_RUN row per verify run (D42): the metrics above, keyed to the git
-- commit and generation seed, in the schema no consumer role can read -- the
-- numbers are computed from labels.
--
-- Binds: run_id, suite_name, profile, git_commit, generation_seed,
-- metrics (JSON text), passed, started_at.
-- ===========================================================================
INSERT INTO {{ fq.ground_truth }}.EVAL_RUN (
    run_id, suite_name, profile, git_commit, generation_seed, metrics, passed,
    started_at, finished_at
)
SELECT ?, ?, ?, ?, ?, PARSE_JSON(?), ?, ?::TIMESTAMP_NTZ, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ;
