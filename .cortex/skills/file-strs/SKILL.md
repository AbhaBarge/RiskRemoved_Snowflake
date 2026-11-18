---
name: file-strs
description: "Prepare N unfiled STRs for officer sign-off without the Streamlit UI: runs the AML Analyst's four case steps (1 Gather evidence, 2 Draft STR, 3 Run Reviewer, 4 AI STR + PDF) as direct procedure calls, as the app's analyst service user. Use whenever the user says 'file N STRs', 'prepare N STRs', 'run the analyst steps for N cases', 'do the 4 steps for N cases', or '/file-strs <N>'."
---

# File STRs (analyst steps, no UI)

Runs exactly what the Case investigation page does for the maker, case by case,
for the next N eligible cases. It stops at the officer's signature: ATTEST_STR is
the checker's step and is never called here.

## Input

- **N** (required): how many STRs to take to READY_FOR_ATTESTATION. If the user
  did not give a number, ask for one.

## Identity (why not .env)

The script connects with the `[snowflake]` section of `app/.streamlit/secrets.toml`
(user `RC_PUBLIC_APP_USER_DEV`, role `RC_PUBLIC_APP_DEV`): the same service user
the app uses while the analyst is signed in. The repo `.env` key is the admin
(`RC_ADMIN_DEV`) and would bypass the maker's grants, so it is not used. The
script prints the user and role it connected as; never print the key.

## Eligible cases

An open case with a signal and no FILED STR, whose latest STR is none, DRAFT or
REJECTED. Fresh cases (no evidence) are taken first. Partly-worked cases resume:
evidence already there skips step 1; a DRAFT skips to step 3; a REJECTED STR is
redrafted. REVIEWED cases are already waiting for the officer and are skipped.

## Workflow

1. **List** (read-only) to show what will be picked:
   ```
   .venv\Scripts\python.exe .cortex\skills\file-strs\scripts\file_strs.py --list
   ```
   Run from the repo root. If fewer eligible cases exist than N, say so.

2. **Stop and confirm** before running. Tell the user which cases will be processed,
   that each takes about a minute (step 4 runs the STR agent), and that each one
   counts toward the AI STR cap of 30 per day (`conf/base.yml` cortex).

3. **Run** (long: use run_in_background and poll with bash_output):
   ```
   .venv\Scripts\python.exe .cortex\skills\file-strs\scripts\file_strs.py --count <N>
   ```
   Each case prints its steps. A failing case (refused procedure, Reviewer rejection,
   AI STR not READY_FOR_ATTESTATION) is reported and skipped; the script takes the
   next eligible case until N are ready or none remain. Exit code 2 = fewer than N.

4. **Report** from the final `RESULT {...}` line: a table of case, AI STR status,
   drafter, both reviewer decisions and PDF path (PDFs go to `out/str_pdfs/`), plus
   any failed cases with their message. Next step: the officer signs on the Officer
   sign-off page. Mention that `riskcopilot -p dev app reset-cases --yes` restores
   demo cases if this was a test.

## Troubleshooting

- `Schema ... does not exist or not authorized`: the secrets file is for another
  account or role; check its `[snowflake]` section (names only).
- AI STR `REFUSED`: the daily spend cap was hit; retry tomorrow or lower N.
