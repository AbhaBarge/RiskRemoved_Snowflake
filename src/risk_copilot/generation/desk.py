"""The branch cash desk -- decisions D4 and D27.

A bank books cash against its till, never party to party: a deposit is
Dr cash desk / Cr customer. Two populations cross the desk:

* injected structuring deposits (``inject``), and
* every AMLworld pair whose payment format is in
  ``internal_accounts.cash_desk.routes_payment_formats`` (``reskin``), posted as
  the payee's regional desk -> the payee. The payer's side is not booked (D30).

Routing only the injected deposits made "counterparty is the cash desk" select
exactly the planted rows (measured: 196 of 196 injected legs, 0 of 9,862 source
legs, before D27), a label proxy the column and value scans cannot see.

The desk is the one deliberate exception to "never mint ids", kept narrow: its
ids still derive through :mod:`risk_copilot.ids` from keys that cannot collide
with AMLworld's, and it appears only in ``ID_MAP_PARTY`` / ``ID_MAP_ACCOUNT``,
never in a customer table.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from risk_copilot.generation import GenerationError
from risk_copilot.generation.draws import weighted_options
from risk_copilot.ids import account_id, party_id

ID_MAP_PARTY_SCHEMA = pa.schema(
    [("party_id", pa.string()), ("src_entity_id", pa.string()), ("generation_seed", pa.int64())]
)
ID_MAP_ACCOUNT_SCHEMA = pa.schema(
    [
        ("account_id", pa.string()),
        ("src_bank_id", pa.string()),
        ("src_account_number", pa.string()),
        ("party_id", pa.string()),
        ("generation_seed", pa.int64()),
    ]
)


@dataclass(frozen=True)
class CashDesk:
    """The desk's identities, one account per branch region."""

    party_id: str
    owner_key: str
    src_bank_id: str
    #: region -> (src_account_number, account_id)
    accounts: Mapping[str, tuple[str, str]]
    routed_formats: frozenset[str]
    seed: int

    @classmethod
    def from_config(cls, generation: Mapping[str, Any]) -> CashDesk:
        try:
            seed = int(generation["seed"])
            internal = generation["internal_accounts"]
            bank = str(internal["src_bank_id"])
            owner = str(internal["owner_entity_key"])
            desk = internal["cash_desk"]
            prefix = str(desk["account_number_prefix"])
            routed = frozenset(str(f) for f in desk["routes_payment_formats"] or ())
            regions = weighted_options(generation["entities"]["branch_regions"], "branch_regions")
        except KeyError as exc:
            raise GenerationError(f"internal_accounts: missing setting {exc}") from exc
        if desk.get("per") != "branch_region":
            raise GenerationError(
                "internal_accounts.cash_desk.per: only branch_region is implemented"
            )
        accounts = {
            code: (f"{prefix}:{code}", account_id(bank, f"{prefix}:{code}", seed=seed))
            for code, _ in regions
        }
        return cls(party_id(owner, seed=seed), owner, bank, accounts, routed, seed)

    def account(self, region: str) -> str:
        try:
            return self.accounts[region][1]
        except KeyError as exc:
            raise GenerationError(f"No cash desk for branch region {region!r}") from exc

    def id_map_party(self) -> pa.Table:
        row = {
            "party_id": self.party_id,
            "src_entity_id": self.owner_key,
            "generation_seed": self.seed,
        }
        return pa.Table.from_pylist([row], schema=ID_MAP_PARTY_SCHEMA)

    def id_map_account(self) -> pa.Table:
        rows = [
            {
                "account_id": aid,
                "src_bank_id": self.src_bank_id,
                "src_account_number": number,
                "party_id": self.party_id,
                "generation_seed": self.seed,
            }
            for _, (number, aid) in sorted(self.accounts.items())
        ]
        return pa.Table.from_pylist(rows, schema=ID_MAP_ACCOUNT_SCHEMA)
