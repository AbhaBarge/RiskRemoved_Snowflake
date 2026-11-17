"""SPCS entrypoint: write the key secrets to files, then start the Slack bot.

The service spec maps Snowflake secrets to env vars. The bot reads key *paths*,
so the PEM text in SNOWFLAKE_PRIVATE_KEY_PEM / AGENT_PRIVATE_KEY_PEM is written
to a private temp dir and the matching *_PATH variable is pointed at it.
"""

from __future__ import annotations

import os
import runpy
import stat
import sys
import tempfile
from pathlib import Path

KEYS = {
    "SNOWFLAKE_PRIVATE_KEY_PEM": "SNOWFLAKE_PRIVATE_KEY_PATH",
    "AGENT_PRIVATE_KEY_PEM": "AGENT_PRIVATE_KEY_PATH",
}

key_dir = Path(tempfile.mkdtemp(prefix="rc_keys_"))
for pem_var, path_var in KEYS.items():
    pem = os.environ.pop(pem_var, "")
    if not pem.strip():
        raise SystemExit(f"{pem_var} is empty: check the service spec secrets")
    path = key_dir / f"{path_var.lower()}.p8"
    path.write_text(pem.replace("\\n", "\n").strip() + "\n")
    path.chmod(stat.S_IRUSR)
    os.environ[path_var] = str(path)

sys.path.insert(0, "/app/bridge")  # run_path does not add the script's folder
runpy.run_path("/app/bridge/slack_bot.py", run_name="__main__")
