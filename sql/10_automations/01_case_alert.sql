-- ===========================================================================
-- 10_automations / 01_case_alert.sql
-- ===========================================================================
-- Unattended notification: email the compliance desk when a case opens or the
-- Principal Officer files an STR.
--
--   NOTIFY_CASE_EVENTS(since, until)  composes and sends one email for the
--     events in (since, until], then writes an AGENT_ACTION_LOG row. Callable
--     on its own, so the email can be shown without waiting for the alert.
--   CASE_EVENTS_ALERT  a serverless Snowflake ALERT: every minute, if a case
--     opened or an STR was filed since its last successful run, it calls the
--     procedure for exactly that window.
--
-- The email names case ids, branch and type, never a customer's name or a
-- figure: it points the reader to the governed record. The recipient is read
-- from conf/external.yml:notifications.case_alert.recipient_env at render time
-- and is never committed; when that variable is unset this file deploys
-- nothing. The alert is created SUSPENDED and resumed only where
-- notifications.case_alert.resume says so, like the routing task.
-- ===========================================================================
{% set A = case_alert %}
{% if A.recipient %}
USE ROLE ACCOUNTADMIN;

CREATE NOTIFICATION INTEGRATION IF NOT EXISTS {{ A.integration }}
    TYPE = EMAIL
    ENABLED = TRUE
    ALLOWED_RECIPIENTS = ({{ A.recipient_literal }})
    COMMENT = 'Risk Copilot ({{ profile }}): case-event emails to the compliance desk.';
ALTER NOTIFICATION INTEGRATION {{ A.integration }} SET ALLOWED_RECIPIENTS = ({{ A.recipient_literal }});
GRANT USAGE ON INTEGRATION {{ A.integration }} TO ROLE {{ role.admin }};
GRANT EXECUTE ALERT ON ACCOUNT TO ROLE {{ role.admin }};
GRANT EXECUTE MANAGED ALERT ON ACCOUNT TO ROLE {{ role.admin }};
GRANT DATABASE ROLE SNOWFLAKE.ALERT_VIEWER TO ROLE {{ role.admin }};

USE ROLE {{ role.admin }};

CREATE OR REPLACE PROCEDURE {{ fq.evidence }}.{{ A.procedure }}(SINCE TIMESTAMP_NTZ, UNTIL TIMESTAMP_NTZ)
    RETURNS VARIANT
    LANGUAGE SQL
    COMMENT = 'Emails the case events in (since, until] and logs the send. Called by the case-event alert.'
    EXECUTE AS OWNER
AS
$$
DECLARE
    opened INTEGER;
    opened_list VARCHAR;
    filed INTEGER;
    filed_list VARCHAR;
    subject VARCHAR;
    body VARCHAR;
BEGIN
    SELECT COUNT(*),
           LISTAGG('  ' || case_id || '  ' || branch_region || ' / ' || case_type || ' / ' || status, '\n')
               WITHIN GROUP (ORDER BY opened_at)
      INTO :opened, :opened_list
      FROM {{ fq.evidence }}.CASE_RECORD
     WHERE opened_at > :since AND opened_at <= :until;

    SELECT COUNT(*),
           LISTAGG('  ' || f.case_id || '  STR ' || t.artifact_id || ', signed hash ' || LEFT(t.attested_content_hash, 16), '\n')
               WITHIN GROUP (ORDER BY t.attested_at)
      INTO :filed, :filed_list
      FROM {{ fq.evidence }}.ATTESTATION t
      JOIN {{ fq.evidence }}.ARTIFACT a ON a.artifact_id = t.artifact_id
      JOIN {{ fq.evidence }}.FINDING f ON f.finding_id = a.finding_id
     WHERE t.decision = 'ATTESTED' AND t.attested_at > :since AND t.attested_at <= :until;

    IF (opened + filed = 0) THEN
        RETURN OBJECT_CONSTRUCT('sent', FALSE, 'cases_opened', 0, 'strs_filed', 0);
    END IF;

    subject := 'Risk Copilot: ' || opened || ' case(s) opened, ' || filed || ' STR(s) filed';
    body := 'Case events between ' || TO_VARCHAR(:since, 'YYYY-MM-DD HH24:MI') || ' and '
         || TO_VARCHAR(:until, 'YYYY-MM-DD HH24:MI') || '.\n\n'
         || 'Cases opened (' || opened || '):\n' || COALESCE(opened_list, '  none') || '\n\n'
         || 'STRs filed by the Principal Officer (' || filed || '):\n' || COALESCE(filed_list, '  none') || '\n\n'
         || 'Open the Risk & Compliance Copilot for the evidence. This message carries ids only, '
         || 'no customer data.';

    CALL SYSTEM$SEND_EMAIL('{{ A.integration }}', {{ A.recipient_literal }}, :subject, :body);

    INSERT INTO {{ fq.audit }}.AGENT_ACTION_LOG (
        log_id, session_id, turn_number, agent_mode, invoked_by, snowflake_user, user_role,
        question_text, tools_invoked, model_version, prompt_version, outcome
    )
    SELECT 'LOG-' || UUID_STRING(), CURRENT_SESSION()::VARCHAR, 1, 'SENTINEL', 'AUTOMATION',
           CURRENT_USER(), CURRENT_ROLE(), :subject,
           ARRAY_CONSTRUCT('{{ A.procedure }}', 'SYSTEM$SEND_EMAIL'), 'NONE', 'CASE_ALERT_V1', 'SUCCESS';

    RETURN OBJECT_CONSTRUCT('sent', TRUE, 'cases_opened', opened, 'strs_filed', filed);
END;
$$;

CREATE OR REPLACE ALERT {{ fq.evidence }}.{{ A.alert }}
    SCHEDULE = {{ A.schedule }}
    COMMENT = 'Emails the compliance desk when a case opens or an STR is filed. Serverless.'
    IF (EXISTS (
        SELECT 1 FROM {{ fq.evidence }}.CASE_RECORD
         WHERE opened_at > SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME()::TIMESTAMP_NTZ
           AND opened_at <= SNOWFLAKE.ALERT.SCHEDULED_TIME()::TIMESTAMP_NTZ
        UNION ALL
        SELECT 1 FROM {{ fq.evidence }}.ATTESTATION
         WHERE decision = 'ATTESTED'
           AND attested_at > SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME()::TIMESTAMP_NTZ
           AND attested_at <= SNOWFLAKE.ALERT.SCHEDULED_TIME()::TIMESTAMP_NTZ
    ))
    THEN
        CALL {{ fq.evidence }}.{{ A.procedure }}(
            SNOWFLAKE.ALERT.LAST_SUCCESSFUL_SCHEDULED_TIME()::TIMESTAMP_NTZ,
            SNOWFLAKE.ALERT.SCHEDULED_TIME()::TIMESTAMP_NTZ
        );

{% if A.resume %}
ALTER ALERT {{ fq.evidence }}.{{ A.alert }} RESUME;
{% endif %}
{% endif %}
