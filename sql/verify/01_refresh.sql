-- ===========================================================================
-- verify / 01_refresh.sql
-- ===========================================================================
-- Refresh every signal table a registered rule writes to. Snowflake refreshes
-- the listed tables and all their upstream dynamic tables as one pipeline, at
-- one data timestamp, so the check reads what the current RAW data produces.
-- Cannot run inside a transaction.
-- ===========================================================================
{% from "verify/_macros.sql" import signal_tables with context %}
ALTER DYNAMIC TABLE
{{ signal_tables() }}
REFRESH;
