# Next steps -- demo polish and public hosting

Working notes for the remaining hackathon work. Read this first in a new session;
`docs/build-log.md` has the history and the measurements (D/M numbers).

## Where things stand (2026-10-02)

- Account ASHWINRAVI004 (ZC14131, AWS ap-southeast-7, Standard **trial**). Dev profile
  only: database `RISK_COPILOT_DEV`; objects carry `_DEV` (tables do not).
- The CLI authenticates with a key pair from `.env` (`RC_SNOWFLAKE_PRIVATE_KEY_PATH`).
  The IDE's own Snowflake connection is a *different* account -- never use it for this
  project.
- The Streamlit app (`app/`) runs in three places from one code path:
  local (`riskcopilot -p dev app run`), Streamlit in Snowflake
  (`riskcopilot -p dev app deploy`), and Streamlit Community Cloud (secrets-based
  session, see below).
- Case workflow: `GATHER_EVIDENCE` -> `DRAFT_STR` (optional planted defect) ->
  `REVIEW_STR` (deterministic) -> `ATTEST_STR` (human gate) -> FILED.
  `RESET_CASES(via, dry_run)` puts every case back to OPEN: a button in
  Governance > Demo controls, `riskcopilot app reset-cases`, or a CALL in Snowsight.
- The narrative stays **templated** (`TEMPLATE_V1`); the trial blocks `AI_COMPLETE`.
  This is final for the hackathon.

## User decisions (2026-10-02)

1. **Demo login.** A login page with the demo credentials shown on the same page,
   so the security model is visible without blocking the demo. There are two
   personas: Analyst (maker) and Principal Officer (checker).
2. **Public hosting.** The app must be reachable on Streamlit Community Cloud with no
   Snowflake account and no login to Snowflake. It connects with a dedicated
   least-privilege service user whose key lives only in Streamlit's secrets.
3. AI narrative: stays templated.
4. Priorities after the demo tasks: (a) a visual-first UI with tables kept as a
   second option, to impress judges; (b) a formal, well-composed look;
   (c) hosting, which is decision 2.

## Work queue (in order)

| # | Task | Status |
|---|---|---|
| 1 | Demo login: `app/lib/auth.py`, `app/lib/login.py`, personas gate the workflow (maker / checker) | done (D57) |
| 2 | Public app role + service user (`sql/11_app_access`, `riskcopilot app public-access`), secrets-based session in `lib/db.py`, `app/requirements.txt`, committed `app/app_config.json` | done (D58-D60), verified live as the service user |
| 3 | Deploy to Streamlit Community Cloud: **user action**, see "Hosting" below | waiting on user |
| 4 | Visual-first UI | done (D61, D62): Overview, Case, Reviewer, STR, Governance; Ask answers drawn automatically (`ui.auto_chart`); KYC fact sheet; sign-off SLA countdown |
| 5 | Formal polish | done (D62): `width="stretch"` everywhere, human column headings in every Table view (`ui.pretty`), one `ui.style()` per page |
| 6 | Data realism: the hero's declared income (about Rs 1,960 crore, from the Phase 2 income derivation) contradicts "Rs 94 lakh is suspicious". Fix in `generation/`, regenerate, then `load --replace` + refresh + route + `verify` | todo, risky: it re-pins the hero |
| 7 | Streaming rehearsal | done (M48-M50, D63-D64): fingerprint equal to the full load, `verify` exit 0. See "Live demo runbook" below |
| 8 | Open items: GATHER double-fire (M47); per-viewer identity in SiS (restricted caller's rights, Streamlit >= 1.53.1) | later |

## Hosting on Streamlit Community Cloud (task 3)

1. Push the branch to GitHub (`ashw1nr/RiskRemoved_Snowflake`). The repository must
   be visible to the Streamlit account.
2. At share.streamlit.io: **Create app** > from GitHub. Pick the repo and the branch,
   and set the main file path to `app/streamlit_app.py`. Under Advanced settings, set
   Python 3.11.
3. Under Advanced settings > **Secrets**, paste the file written by
   `riskcopilot -p dev app public-access`. It is kept outside the repository, under
   `~/.snowflake/keys/`.
4. Deploy. Anyone with the URL can then use the app. The service user can read the
   demo data and run the case tools, and nothing else.

## Live demo runbook (task 7, rehearsed 2026-10-02)

About 6 minutes of streaming; each step was run in the rehearsal (build log Step 12).

1. `riskcopilot -p dev load --replace` (holds back from 2022-09-03), then
   `riskcopilot -p dev refresh`.
2. Clear the 30 chain rows (SIGNAL, CASE_RECORD, CASE_SIGNAL; D46), then
   `CALL ROUTE_SIGNALS_DEV(TRUE)` -- expect 2 / 2 / 2 and an empty stream.
3. `ALTER TASK ROUTE_SIGNALS_TASK_DEV RESUME` (only with the stream empty).
4. Open Overview, turn on **Live mode**.
5. `riskcopilot -p dev replay --inject structuring_burst`, then `riskcopilot -p dev refresh`:
   the demo case appears within about 10 s.
6. `riskcopilot -p dev replay` (32 ticks, about 3 minutes). Run `riskcopilot -p dev refresh`
   once or twice while it streams; each refresh opens new cases on screen within
   about 10 s. Do not run `--inject` while it streams (D47).
7. `riskcopilot -p dev refresh`, wait for the task run, then
   `ALTER TASK ROUTE_SIGNALS_TASK_DEV SUSPEND`.
8. `riskcopilot -p dev verify` (exit 0), then `riskcopilot -p dev app reset-cases`.

Keep the laptop awake while the replay streams (M39). The app now survives an
expired session (D64), but a replay does not.

## Rules to keep

- No literal object names in SQL or the app: names come from `conf/`, through
  `app/app_config.json` for the app. Values are always bound.
- Commit per task, with gates: pytest, ruff, mypy, `python -m risk_copilot.pipeline.lint`.
  Do not push unless asked.
- Never commit keys or `secrets.toml`; `.gitignore` covers both.
- Before the demo: reset the cases, open the deployed app once, and click one sign-off.
