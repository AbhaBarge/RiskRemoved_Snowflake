"""One-off: merge C-style adjacent SQL string literals onto a single line.

Snowflake does not concatenate adjacent string literals the way C does, so a
COMMENT written as::

    COMMENT = 'first part '
              'second part'

is a syntax error rather than one string. This script rewrites those into single
literals. Deployer.lint() now rejects the pattern, so this runs once.
"""

from __future__ import annotations

import pathlib
import re

CONTINUATION_END = re.compile(r"^(.*?)'([,;]?)$")


def merge_file(path: pathlib.Path) -> int:
    lines = path.read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    merged = 0

    for line in lines:
        stripped = line.strip()
        if out and stripped.startswith("'"):
            prev = out[-1].rstrip()
            match = CONTINUATION_END.match(prev)
            if match:
                head = match.group(1)
                fragment = stripped[1:]  # drop the opening quote
                out[-1] = head + ("" if head.endswith(" ") else " ") + fragment
                merged += 1
                continue
        out.append(line)

    if merged:
        path.write_text("\n".join(out), encoding="utf-8")
    return merged


def main() -> None:
    total = 0
    for path in sorted(pathlib.Path("sql").rglob("*.sql")):
        count = merge_file(path)
        if count:
            print(f"{path.as_posix():52} merged {count}")
            total += count
    print(f"total merged: {total}")


if __name__ == "__main__":
    main()
