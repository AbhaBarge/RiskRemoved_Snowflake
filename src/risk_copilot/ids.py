"""Deterministic identity derivation -- the referential backbone.

This module exists because of GAP-02, the most serious defect in the original
plan. The architecture generated ~5K Faker parties with fresh ids while the
transaction ledger referenced IBM AMLworld's hex account ids. Nothing
reconciled the two, so every downstream join would have silently returned
nothing: no declared income (killing INCOME_ACTIVITY_MISMATCH), no device
fingerprint (killing SHARED_DEVICE_CLUSTER), no branch region (killing the
row-access-policy demo).

The fix is to invert the direction of generation. AMLworld's keys are
authoritative; L1 *enriches* them rather than replacing them. Every synthetic
attribute is derived from a stable hash of the source key, which buys three
properties:

* **Referential integrity by construction.** A party id is a pure function of
  an AMLworld ``entity_id``, so a transaction leg can never reference a party
  that generation did not produce.
* **Idempotence.** Re-running generation, or resampling at a different size,
  yields the same id and the same attributes for the same source entity.
* **Reproducible-but-uncorrelated attributes.** Each attribute draws from its
  own derived stream, so risk rating and branch region are independent even
  though both descend from one seed.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

#: Zero-padding widths.
#:
#: Sized against the FULL AMLworld population (~700K entities, 518,581
#: accounts), not the sampled subgraph, so an id stays stable if the sample
#: grows. Width is a collision-probability decision, not a cosmetic one: a
#: collision merges two distinct entities and silently corrupts the counterparty
#: graph, which is precisely the class of failure this module exists to prevent.
#:
#: With n keys drawn uniformly over m values, expected collisions ~= n^2 / 2m.
#: At n = 700,000:
#:     width  7 (m = 1e7)  -> ~24,500 collisions   unusable
#:     width 10 (m = 1e10) -> ~24 collisions       still unacceptable
#:     width 12 (m = 1e12) -> ~0.25 collisions     acceptable
#: Twelve digits is therefore the floor. Generation additionally asserts that the
#: emitted map is 1:1 and fails loudly rather than merging, so a rare collision
#: is a build failure rather than silent corruption.
PARTY_ID_WIDTH = 12
ACCOUNT_ID_WIDTH = 12
TXN_ID_WIDTH = 12

PARTY_ID_PREFIX = "P-"
ACCOUNT_ID_PREFIX = "A-"
TXN_ID_PREFIX = "T-"
PAIR_ID_PREFIX = "TP-"

_PARTY_ID_RE = re.compile(rf"^{PARTY_ID_PREFIX}\d{{{PARTY_ID_WIDTH}}}$")
_ACCOUNT_ID_RE = re.compile(rf"^{ACCOUNT_ID_PREFIX}\d{{{ACCOUNT_ID_WIDTH}}}$")

#: Modulus for surrogate ids. 10**width, so the formatted id never overflows
#: its declared width. Collisions are possible in principle at these widths;
#: the generator asserts uniqueness of the emitted map and fails loudly rather
#: than silently merging two entities.
_PARTY_MOD = 10**PARTY_ID_WIDTH
_ACCOUNT_MOD = 10**ACCOUNT_ID_WIDTH


def stable_hash(*parts: object, namespace: str = "") -> int:
    """Return a stable 64-bit integer digest of ``parts``.

    Uses BLAKE2b rather than :func:`hash`, whose output is salted per process
    and therefore useless for reproducibility.

    Args:
        *parts: Values joined with ``\\x1f`` (unit separator, which cannot occur
            in the source keys) so that ``("a", "bc")`` and ``("ab", "c")``
            digest differently.
        namespace: Domain separator. Distinct namespaces give uncorrelated
            streams from the same inputs, which is what keeps derived
            attributes independent.
    """
    payload = "\x1f".join(str(p) for p in (namespace, *parts))
    return int.from_bytes(hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest(), "big")


def derived_rng(*parts: object, seed: int, namespace: str = "") -> random.Random:
    """Return a :class:`random.Random` seeded deterministically from ``parts``.

    The per-entity generator pattern. Rather than drawing every attribute from
    one global stream -- where output depends on iteration order -- each entity
    gets its own stream keyed by its identity. Attributes are then stable
    regardless of how many entities are generated or in what order.
    """
    return random.Random(stable_hash(seed, *parts, namespace=namespace))  # noqa: S311


# --------------------------------------------------------------------------- #
# Surrogate identifiers
# --------------------------------------------------------------------------- #


def party_id(entity_id: str, *, seed: int) -> str:
    """Derive a stable ``P-#######`` id from an AMLworld ``entity_id``.

    Args:
        entity_id: Hex entity id from ``HI-Small_accounts.csv``.
        seed: Master generation seed.
    """
    if not entity_id or not str(entity_id).strip():
        raise ValueError("entity_id must be a non-empty string")
    n = stable_hash(str(entity_id).strip().upper(), seed, namespace="party") % _PARTY_MOD
    return f"{PARTY_ID_PREFIX}{n:0{PARTY_ID_WIDTH}d}"


def normalise_bank_id(bank_id: object) -> str:
    """Return the canonical form of an AMLworld bank id.

    **Measured, not assumed.** ``HI-Small_Trans.csv`` and ``HI-Small_Patterns.txt``
    zero-pad bank ids (``010``, ``021174``); ``HI-Small_accounts.csv`` does not
    (``10``, ``21174``). Joining the raw strings matched **0 of 300,000** ledger
    rows to an account; after normalisation all 300,000 matched. Every id
    derivation goes through here, so the three files can only ever agree.

    Only purely numeric ids are stripped. A non-numeric key -- such as the
    configured internal bank id for branch cash-desk accounts -- is left intact,
    so it can never be normalised into collision with a real bank.
    """
    text = str(bank_id).strip().upper()
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def account_id(bank_id: str, account_number: str, *, seed: int) -> str:
    """Derive a stable ``A-########`` id from AMLworld's composite account key.

    AMLworld account numbers are unique only within a bank (measured: 8 account
    numbers recur across banks), so the surrogate must derive from
    ``(bank_id, account_number)``. Deriving from the account number alone would
    merge distinct accounts across banks -- silently corrupting the counterparty
    graph. The bank id is normalised first; see :func:`normalise_bank_id`.
    """
    if not account_number or not str(account_number).strip():
        raise ValueError("account_number must be a non-empty string")
    n = (
        stable_hash(
            normalise_bank_id(bank_id),
            str(account_number).strip().upper(),
            seed,
            namespace="account",
        )
        % _ACCOUNT_MOD
    )
    return f"{ACCOUNT_ID_PREFIX}{n:0{ACCOUNT_ID_WIDTH}d}"


def txn_pair_id(sequence: int) -> str:
    """Return the shared id linking a debit leg to its credit leg (GAP-03).

    One AMLworld row becomes two legs. They share this id, which is what
    ``V_TRANSACTION_PAIRS`` self-joins on to recover the pair-level shape that
    graph queries need.
    """
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    return f"{PAIR_ID_PREFIX}{sequence:0{TXN_ID_WIDTH}d}"


def txn_leg_id(pair_id: str, side: str) -> str:
    """Return the leg-level transaction id for one side of a pair.

    Args:
        pair_id: Value from :func:`txn_pair_id`.
        side: ``"D"`` (debit) or ``"C"`` (credit).
    """
    normalised = side.strip().upper()
    if normalised not in {"D", "C"}:
        raise ValueError(f"side must be 'D' or 'C', got {side!r}")
    return f"{TXN_ID_PREFIX}{pair_id.removeprefix(PAIR_ID_PREFIX)}-{normalised}"


def is_party_id(value: str) -> bool:
    """Whether ``value`` has the shape of a generated party id."""
    return bool(_PARTY_ID_RE.match(value or ""))


def is_account_id(value: str) -> bool:
    """Whether ``value`` has the shape of a generated account id."""
    return bool(_ACCOUNT_ID_RE.match(value or ""))


# --------------------------------------------------------------------------- #
# Business keys
# --------------------------------------------------------------------------- #


def signal_business_key(
    rule_id: str, party_id_value: str, window_bucket: str, *, account_id_value: str | None = None
) -> str:
    """Return the deterministic dedup key for a detection signal (GAP-11).

    A dynamic table recomputes its entire result set on every refresh, so the
    same logical signal reappears every five minutes. Without a stable business
    key, the stream plus ``ROUTE_SIGNALS`` task would insert duplicate SIGNAL
    and CASE rows indefinitely -- the original design had no idempotency story
    at all. Detection SQL emits this key and the router ``MERGE``\\ s on it.

    Args:
        rule_id: Firing rule.
        party_id_value: Subject party.
        window_bucket: Canonical observation window (for example ``2022-09``
            for a monthly rule, or an ISO day for a daily rule). What makes the
            key stable across refreshes rather than moving with wall-clock time.
        account_id_value: Present for account-grain rules, omitted for
            party-grain rules.
    """
    for name, value in (
        ("rule_id", rule_id),
        ("party_id", party_id_value),
        ("window_bucket", window_bucket),
    ):
        if not value or not str(value).strip():
            raise ValueError(f"{name} must be non-empty")
    parts = [rule_id.strip().upper(), party_id_value.strip().upper(), window_bucket.strip()]
    if account_id_value:
        parts.append(account_id_value.strip().upper())
    return "|".join(parts)


#: Hex digits of SHA-256 kept in a routed id: 64 bits, so a collision among the
#: few thousand signals a profile can hold (validation.signal_yield) is ~1e-13.
ROUTED_ID_HEX = 16
SIGNAL_ID_PREFIX = "SIG-"
CASE_ID_PREFIX = "CASE-"


def _sha256_prefix(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:ROUTED_ID_HEX]


def signal_id(business_key: str) -> str:
    """Return SIGNAL.signal_id for a business key.

    A pure function of the key, so a replay and a batch run name the same signal
    the same way. ROUTE_SIGNALS computes it in SQL as
    ``'SIG-' || LEFT(SHA2(key, 256), 16)``; the live proof checks both agree.
    """
    if not business_key or not business_key.strip():
        raise ValueError("business_key must be non-empty")
    return f"{SIGNAL_ID_PREFIX}{_sha256_prefix(business_key)}"


def case_id(party_id_value: str, opening_business_key: str) -> str:
    """Return CASE_RECORD.case_id for a party's case, from the signal that opened it.

    The opening key is the smallest business key among the signals that opened
    the case. A party's next case (after this one closes) opens on a signal not
    yet attached to any case, so it gets a different id. SQL:
    ``'CASE-' || LEFT(SHA2(party_id || '|' || key, 256), 16)``.
    """
    for name, value in (("party_id", party_id_value), ("business_key", opening_business_key)):
        if not value or not value.strip():
            raise ValueError(f"{name} must be non-empty")
    return f"{CASE_ID_PREFIX}{_sha256_prefix(f'{party_id_value}|{opening_business_key}')}"


def weighted_choice(rng: random.Random, options: Sequence[tuple[T, float]]) -> T:
    """Pick one option by weight using ``rng``.

    Implemented directly rather than via :func:`random.choices` so the draw
    consumes exactly one value from the stream. That keeps a downstream
    attribute's value from shifting when an unrelated option list is reordered.
    """
    if not options:
        raise ValueError("options must not be empty")
    total = sum(weight for _, weight in options)
    if total <= 0:
        raise ValueError("option weights must sum to a positive value")
    threshold = rng.random() * total
    cumulative = 0.0
    for value, weight in options:
        cumulative += weight
        if threshold < cumulative:
            return value
    return options[-1][0]
