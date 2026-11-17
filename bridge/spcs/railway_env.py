"""Write bridge/railway.env: every variable the hosted bot needs, ready to paste
into Railway (service > Variables > Raw Editor).

The .p8 keys are inlined as SNOWFLAKE_PRIVATE_KEY_PEM / AGENT_PRIVATE_KEY_PEM
(newlines escaped as \\n; bridge/spcs/entrypoint.py turns them back into files).
The output file holds secrets: it is gitignored, and nothing is printed.

    python bridge/spcs/railway_env.py
"""

from __future__ import annotations

from deploy import BRIDGE, ENV, PASS_THROUGH_SKIP, SECRET_ENV, _pem

OUT = BRIDGE / "railway.env"


def main() -> None:
    env = {k: v for k, v in ENV.items() if k not in PASS_THROUGH_SKIP - SECRET_ENV and v}
    env["DEMO_MODE"] = ENV.get("DEMO_MODE", "false")
    env["SNOWFLAKE_PRIVATE_KEY_PEM"] = _pem("SNOWFLAKE_PRIVATE_KEY_PATH").strip().replace("\n", "\\n")
    env["AGENT_PRIVATE_KEY_PEM"] = _pem("AGENT_PRIVATE_KEY_PATH").strip().replace("\n", "\\n")
    for k, v in env.items():
        if "\n" in v or "\r" in v:
            raise SystemExit(f"{k}: multi-line value, cannot be written as one KEY=value line")
    # Unquoted KEY=value: valid for Railway's Raw Editor and for `docker run --env-file`.
    OUT.write_text("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
    print(f"wrote {OUT} ({len(env)} variables; contains secrets, do not commit or share)")


if __name__ == "__main__":
    main()
