"""Provisional demo hero selection -- GAP-18, plan step 6.

The docs hardcoded ``P-10042 / Rajesh Kumar``; ids are generated, so that script
would be fiction. Here the hero is *selected*: a party whose evaluation grain
expects every rule in ``demo_entity.must_trigger_rules``, i.e. one that was
**constructed** to trip each of them, preferring the configured party type and
branch region, ranked by ``stable_hash`` within that preference.

Whether the rules actually fire is only knowable after load, in Snowflake (D7).
The pin is therefore written with ``status: PROVISIONAL_UNTIL_DETECTED``; the
post-load check promotes or rejects it. With ``fail_if_not_found`` a profile
whose listed rules have no constructed cohort -- demo, until rapid-credit and
device-cluster cohorts are injected -- fails ``generate`` rather than pinning a
party that cannot demonstrate the story.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.ids import stable_hash

STATUS = "PROVISIONAL_UNTIL_DETECTED"


class DemoEntityError(GenerationError):
    """No party was constructed to trip every rule the demo needs."""


def select_demo_entity(
    eval_grain: pa.Table,
    cbs: pa.Table,
    generation: Mapping[str, Any],
    *,
    profile: str,
) -> dict[str, Any] | None:
    """Return the pin for ``conf/generated/demo_entity.<profile>.yml``.

    Returns ``None`` only when nothing qualifies and ``fail_if_not_found`` is
    false.
    """
    spec = generation["demo_entity"]
    must = sorted(str(r) for r in spec["must_trigger_rules"])
    seed = int(generation["seed"])
    expected: dict[str, set[str]] = {}
    for row in eval_grain.select(["party_id", "expected_rule_ids"]).to_pylist():
        expected.setdefault(row["party_id"], set()).update(row["expected_rule_ids"])
    parties = {r["party_id"]: r for r in cbs.to_pylist()}
    candidates = sorted(p for p, rules in expected.items() if set(must) <= rules and p in parties)

    if not candidates:
        if spec.get("fail_if_not_found", True):
            raise DemoEntityError(
                f"No party was constructed to trigger all of {must} (profile {profile!r}). "
                "Enable the missing injection cohorts, or narrow "
                "demo_entity.must_trigger_rules for this profile."
            )
        return None

    def rank(pid: str) -> tuple[int, int, int, str]:
        party = parties[pid]
        return (
            0 if party["party_type"] == spec.get("prefer_party_type") else 1,
            0 if party["branch_region"] == spec.get("prefer_branch_region") else 1,
            stable_hash(pid, seed, namespace="demo-entity"),
            pid,
        )

    hero = parties[min(candidates, key=rank)]
    return {
        "status": STATUS,
        "profile": profile,
        "generation_seed": seed,
        "party_id": hero["party_id"],
        "full_name": hero["full_name"],
        "party_type": hero["party_type"],
        "branch_region": hero["branch_region"],
        "must_trigger_rules": must,
        "candidates_considered": len(candidates),
        "note": (
            "Selected because generation constructed this party to trip every listed rule. "
            "Promote to CONFIRMED only after the post-load detection check sees each rule fire."
        ),
    }
