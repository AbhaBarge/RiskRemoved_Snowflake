"""``riskcopilot replay`` -- the held-back legs, streamed in as if live (Phase 3, step 8).

``load`` holds back every leg timestamped at or after ``replay.hold_back_from``.
This module brings them in afterwards, in timestamp order, one simulated
``sim_step`` per ``sleep_seconds``:

1. **The plan is made offline**, from the hash-verified bundle, so it is the same
   on every run and testable without a connection. A tick covers ``[start, end)``
   on the ``sim_step`` grid from the cut, and ends early, at a timestamp boundary,
   when it would exceed ``batch_legs``. Legs sharing a timestamp stay together;
   no pair has legs at two timestamps (M36), so no pair is ever split.
2. **Source legs** come from the bundle's own RAW_TRANSACTIONS file: staged under
   its content-addressed path, copied into a session TEMPORARY table with the
   loader's COPY, then ``INSERT ... SELECT`` anti-joined on ``txn_id``.
   ``ingested_at`` is left to its default, so it records the replayed arrival.
3. **Injected legs** are promoted as their tick arrives, through
   :meth:`Loader.promote` -- the same SQL ``load`` runs, narrowed to the window.
4. **Each tick is one transaction.** A restart skips the ticks already complete,
   and the anti-joins make a re-run tick insert nothing twice.

``replay --inject <name>`` (:func:`inject`) releases one host's pending cohort
legs immediately: data ``generate`` already wrote and labelled, never new data.

Replay never refreshes a dynamic table and never routes. The target lag (or
``verify``) refreshes, and the routing task routes what the refresh changed.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow.parquet as pq

from risk_copilot.config import ConfigError, Settings, validate_identifier
from risk_copilot.pipeline.load import (
    PENDING,
    TRANSACTIONS,
    TXN_TIMESTAMP,
    Bundle,
    Check,
    Loader,
    LoadError,
    PendingFilter,
    bind_timestamp,
    check_bundle,
    copy_parquet_sql,
    select_expressions,
    window_filters,
)

#: Session-private copy of the bundle's source legs. TEMPORARY, so it shadows
#: nothing and disappears with the session.
REPLAY_SOURCE = "REPLAY_SOURCE_LEGS"
#: The RAW table that says which party owns an account.
ACCOUNTS = "RAW_ACCOUNTS"


class ReplayError(RuntimeError):
    """Replay was refused or failed; the tick in progress was rolled back."""


def _roll_back(sql: Callable[[str], Any], exc: BaseException) -> BaseException:
    """ROLLBACK after ``exc``; if the ROLLBACK itself fails, report both.

    A lost session (measured: the machine slept mid-replay and the session
    token expired) fails the ROLLBACK too, and its error must not hide the
    original one. Snowflake aborts an uncommitted transaction when its session
    ends, so nothing of the failed tick is committed either way.
    """
    try:
        sql("ROLLBACK")
    except Exception as rollback:
        return ReplayError(
            f"{exc}\nROLLBACK also failed ({rollback}); the session's uncommitted "
            "transaction is aborted by Snowflake when the session ends. Re-run to resume."
        )
    return exc


# --------------------------------------------------------------------------- #
# The plan (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Leg:
    timestamp: dt.datetime
    txn_id: str
    #: True for a source leg (RAW file), False for an injected one (pending).
    source: bool


@dataclass(frozen=True)
class Tick:
    index: int
    start: dt.datetime
    end: dt.datetime
    source_ids: frozenset[str]
    pending_ids: frozenset[str]

    @property
    def legs(self) -> int:
        return len(self.source_ids) + len(self.pending_ids)


def held_back_legs(bundle: Bundle, cut: dt.datetime) -> list[Leg]:
    """Every bundle leg at or after ``cut``, in (timestamp, txn_id) order."""
    legs = []
    for zone, table, source in (("raw", TRANSACTIONS, True), ("ground_truth", PENDING, False)):
        data = pq.read_table(bundle.artifact(zone, table).path, columns=["txn_id", TXN_TIMESTAMP])
        for txn_id, stamp in zip(
            data.column(0).to_pylist(), data.column(1).to_pylist(), strict=True
        ):
            if stamp >= cut:
                legs.append(Leg(stamp, str(txn_id), source))
    return sorted(legs, key=lambda leg: (leg.timestamp, leg.txn_id))


def _whole_groups(legs: Sequence[Leg], i: int, j: int, cap: int) -> int:
    """End index of the longest run of whole timestamp groups from ``i`` within ``cap``.

    At least one group is taken, even if it alone exceeds ``cap``: splitting a
    timestamp would split a pair.
    """
    k = i
    while k < j:
        g = k
        while g < j and legs[g].timestamp == legs[k].timestamp:
            g += 1
        if g - i > cap and k > i:
            break
        k = g
    return k


def plan_ticks(
    legs: Sequence[Leg], cut: dt.datetime, step_seconds: int, batch_legs: int
) -> list[Tick]:
    """Consecutive ``[start, end)`` ticks from ``cut`` covering ``legs`` (sorted)."""
    if step_seconds <= 0 or batch_legs < 1:
        raise ReplayError("sim_step must be positive and batch_legs at least 1")
    step = dt.timedelta(seconds=step_seconds)
    ticks: list[Tick] = []
    start, i = cut, 0
    while i < len(legs):
        # The next grid point after `start`: a tick cut early resumes the same step.
        boundary = cut + ((start - cut) // step + 1) * step
        j = i
        while j < len(legs) and legs[j].timestamp < boundary:
            j += 1
        end = boundary
        if j - i > batch_legs:
            k = _whole_groups(legs, i, j, batch_legs)
            if k < j:
                end, j = legs[k].timestamp, k
        taken = legs[i:j]
        ticks.append(
            Tick(
                index=len(ticks),
                start=start,
                end=end,
                source_ids=frozenset(leg.txn_id for leg in taken if leg.source),
                pending_ids=frozenset(leg.txn_id for leg in taken if not leg.source),
            )
        )
        start, i = end, j
    return ticks


# --------------------------------------------------------------------------- #
# SQL builders (pure)
# --------------------------------------------------------------------------- #


def source_insert_sql(fq_transactions: str, fq_source: str, columns: Sequence[str]) -> str:
    """One tick of source legs. Binds: window start, window end.

    ``columns`` are the bundle file's, so ``ingested_at`` keeps its default.
    """
    cols = ", ".join(columns)
    return (
        f"INSERT INTO {fq_transactions} ({cols})\n"
        f"SELECT {cols}\nFROM {fq_source} s\n"
        f"WHERE s.{TXN_TIMESTAMP} >= ?::TIMESTAMP_NTZ AND s.{TXN_TIMESTAMP} < ?::TIMESTAMP_NTZ\n"
        f"  AND NOT EXISTS (SELECT 1 FROM {fq_transactions} r WHERE r.txn_id = s.txn_id)"
    )


def host_filters(
    label: str, fq_pending: str, fq_accounts: str, party_id: str
) -> list[PendingFilter]:
    """Pending legs of cohort ``label`` on the pairs whose credit leg a party's account holds.

    Selected by pair, so the cash-desk debit of each deposit goes with it.
    """
    return [
        PendingFilter("{p}cohort = ?", (label,)),
        PendingFilter(
            "{p}txn_pair_id IN (SELECT q.txn_pair_id FROM "
            f"{fq_pending} q JOIN {fq_accounts} a ON a.account_id = q.account_id "
            "WHERE a.party_id = ?)",
            (party_id,),
        ),
    ]


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TickResult:
    tick: Tick
    source: int
    promoted: int
    #: Every leg was already in RAW (an earlier, interrupted run).
    skipped: bool


@dataclass
class ReplayReport:
    cut: dt.datetime
    held_legs: int
    ticks: list[Tick]
    results: list[TickResult] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


class Replayer:
    """Stream one bundle's held-back legs. See the module docstring."""

    def __init__(
        self,
        settings: Settings,
        session: Any,
        bundle: Bundle,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        spec = settings.replay
        if spec.hold_back_from is None:
            raise ReplayError(
                f"replay.hold_back_from is null for profile {settings.profile!r}: load holds "
                "nothing back, so there is nothing to replay."
            )
        self.settings = settings
        self.session = session
        self.bundle = bundle
        self.spec = spec
        self.cut: dt.datetime = spec.hold_back_from
        self.sleep = sleep
        self.loader = Loader(settings, session, source=False, hold_back=self.cut)
        self.fq_tx = self.loader.table("raw", TRANSACTIONS)
        self.fq_source = f"{self.loader.fq['raw']}.{REPLAY_SOURCE}"
        self.fq_pending = self.loader.table("ground_truth", PENDING)

    def _count(self, sql: str, params: Sequence[Any] | None = None) -> int:
        return int(self.loader.scalar(sql, params) or 0)

    def plan(self) -> list[Tick]:
        legs = held_back_legs(self.bundle, self.cut)
        return plan_ticks(legs, self.cut, self.spec.sim_step_seconds, self.spec.batch_legs)

    def stage_source(self) -> list[str]:
        """The bundle's RAW_TRANSACTIONS in a TEMPORARY table; returns its columns."""
        staged = self.loader.upload_artifact(self.bundle, "raw", TRANSACTIONS)
        target = self.loader.columns("raw").get(TRANSACTIONS)
        if not target:
            raise ReplayError(f"{self.fq_tx} does not exist; deploy ddl and load first")
        path = self.bundle.artifact("raw", TRANSACTIONS).path
        schema = pq.read_schema(path)
        exprs = select_expressions(schema, target, TRANSACTIONS)
        names = [target[validate_identifier(f.name, what="File column")].name for f in schema]
        # The file's columns at RAW's exact types, without RAW's constraints or
        # ingested_at: a zero-row CTAS rather than LIKE.
        self.loader.sql(
            f"CREATE OR REPLACE TEMPORARY TABLE {self.fq_source} AS "
            f"SELECT {', '.join(names)} FROM {self.fq_tx} LIMIT 0"
        )
        self.loader.sql(
            copy_parquet_sql(self.fq_source, names, staged, self.loader.parquet_format, exprs)
        )
        copied = self._count(f"SELECT COUNT(*) FROM {self.fq_source}")
        expected = self.bundle.artifact("raw", TRANSACTIONS).rows
        if copied != expected:
            raise ReplayError(
                f"{self.fq_source}: copied {copied} source legs, the bundle has {expected}"
            )
        return names

    def preflight(self) -> None:
        """Refuse data this bundle did not write, and a load that held back differently."""
        foreign = self._count(
            f"SELECT COUNT(*) FROM {self.fq_tx} r "
            f"WHERE NOT EXISTS (SELECT 1 FROM {self.fq_source} s WHERE s.txn_id = r.txn_id) "
            f"AND NOT EXISTS (SELECT 1 FROM {self.fq_pending} p WHERE p.txn_id = r.txn_id)"
        )
        if foreign:
            raise ReplayError(
                f"{self.fq_tx} holds {foreign} legs this bundle did not write: it was loaded "
                "from another build. Load this bundle before replaying it."
            )
        cut = [bind_timestamp(self.cut)]
        missing = self._count(
            f"SELECT COUNT(*) FROM {self.fq_source} s WHERE s.{TXN_TIMESTAMP} < ?::TIMESTAMP_NTZ "
            f"AND NOT EXISTS (SELECT 1 FROM {self.fq_tx} r WHERE r.txn_id = s.txn_id)",
            cut,
        )
        unpromoted = self._count(
            f"SELECT COUNT(*) FROM {self.fq_pending} "
            f"WHERE resolved_at IS NULL AND {TXN_TIMESTAMP} < ?::TIMESTAMP_NTZ",
            cut,
        )
        if missing or unpromoted:
            raise ReplayError(
                f"{missing} source and {unpromoted} injected legs before {self.cut} are not "
                "loaded: load ran with a later hold-back, or not at all. Load first."
            )

    def _present(self) -> set[str]:
        rows = self.loader.sql(
            f"SELECT txn_id FROM {self.fq_tx} WHERE {TXN_TIMESTAMP} >= ?::TIMESTAMP_NTZ",
            [bind_timestamp(self.cut)],
        )
        return {str(r[0]) for r in rows}

    def _tick(self, tick: Tick, columns: Sequence[str], present: set[str]) -> TickResult:
        expected = len(tick.source_ids - present)
        self.loader.sql("BEGIN")
        try:
            rows = self.loader.sql(
                source_insert_sql(self.fq_tx, self.fq_source, columns),
                [bind_timestamp(tick.start), bind_timestamp(tick.end)],
            )
            inserted = int(rows[0][0]) if rows else 0
            if inserted != expected:
                raise ReplayError(
                    f"Tick {tick.index}: inserted {inserted} source legs, planned {expected}"
                )
            promoted = self.loader.promote(window_filters(tick.start, tick.end))
        except BaseException as exc:
            raised = _roll_back(self.loader.sql, exc)
            if raised is exc:
                raise
            raise raised from exc
        self.loader.sql("COMMIT")
        return TickResult(tick, inserted, promoted, skipped=False)

    def run(self, on_tick: Callable[[TickResult, int], None] | None = None) -> ReplayReport:
        check_bundle(self.bundle, self.settings)
        ticks = self.plan()
        report = ReplayReport(self.cut, sum(t.legs for t in ticks), ticks)
        columns = self.stage_source()
        self.preflight()
        present = self._present()
        pending_ids = {i for t in ticks for i in t.pending_ids}
        executed = 0
        for tick in ticks:
            todo = (tick.source_ids | tick.pending_ids) - present
            if not todo:
                result = TickResult(tick, 0, 0, skipped=True)
            else:
                if executed:
                    self.sleep(self.spec.sleep_seconds)
                result = self._tick(tick, columns, present)
                executed += 1
            report.results.append(result)
            if on_tick:
                on_tick(result, len(ticks))
        report.checks = self._verify(report.held_legs, len(pending_ids))
        return report

    def _verify(self, held: int, pending: int) -> list[Check]:
        cut = [bind_timestamp(self.cut)]
        arrived = self._count(
            f"SELECT COUNT(*) FROM {self.fq_tx} WHERE {TXN_TIMESTAMP} >= ?::TIMESTAMP_NTZ", cut
        )
        waiting = self._count(f"SELECT COUNT(*) FROM {self.fq_pending} WHERE resolved_at IS NULL")
        duplicated = self._count(f"SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM {self.fq_tx}")
        return [
            Check("held-back legs arrived", arrived == held, f"{arrived:,}", f"planned {held:,}"),
            Check(
                "injected legs promoted",
                waiting == 0,
                f"{waiting} unresolved",
                f"{pending} held back",
            ),
            Check("no leg twice", duplicated == 0, f"{duplicated} duplicate txn_ids"),
        ]


def replay(
    settings: Settings,
    session: Any,
    bundle: Bundle,
    *,
    on_tick: Callable[[TickResult, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplayReport:
    """Stream ``bundle``'s held-back legs through ``session``."""
    try:
        return Replayer(settings, session, bundle, sleep=sleep).run(on_tick)
    except (ConfigError, LoadError) as exc:
        raise ReplayError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# --inject
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InjectReport:
    name: str
    cohort: str
    party_id: str
    promoted: int
    first: Any
    last: Any


def cohort_label(settings: Settings, name: str) -> str:
    """The pending ``cohort`` value a ``--inject`` name releases."""
    injections = settings.replay.injections
    if name not in injections:
        raise ReplayError(f"Unknown --inject {name!r}; configured: {sorted(injections)}")
    return str(settings.generation["injection"][injections[name]]["label"])


def inject(settings: Settings, session: Any, name: str, party_id: str) -> InjectReport:
    """Release ``party_id``'s pending ``name`` legs now, in one transaction."""
    try:
        label = cohort_label(settings, name)
        loader = Loader(settings, session, source=False)
        fq_pending = loader.table("ground_truth", PENDING)
        fq_accounts = loader.table("raw", ACCOUNTS)
        filters = host_filters(label, fq_pending, fq_accounts, party_id)
        where = " AND ".join(f.render() for f in filters)
        params = [v for f in filters for v in f.params]
        total, waiting, first, last = loader.sql(
            f"SELECT COUNT(*), COUNT_IF(resolved_at IS NULL), "
            f"MIN(IFF(resolved_at IS NULL, {TXN_TIMESTAMP}, NULL)), "
            f"MAX(IFF(resolved_at IS NULL, {TXN_TIMESTAMP}, NULL)) "
            f"FROM {fq_pending} WHERE {where}",
            params,
        )[0]
        if not total:
            raise ReplayError(f"{party_id} holds no {label!r} legs: it is not a host of {name}")
        if not waiting:
            raise ReplayError(
                f"{party_id}'s {total} {label!r} legs are all in RAW already: released earlier, "
                "or loaded without a hold-back"
            )
        loader.sql("BEGIN")
        try:
            promoted = loader.promote(filters)
        except BaseException as exc:
            raised = _roll_back(loader.sql, exc)
            if raised is exc:
                raise
            raise raised from exc
        loader.sql("COMMIT")
    except (ConfigError, LoadError) as exc:
        raise ReplayError(str(exc)) from exc
    return InjectReport(name, label, party_id, promoted, first, last)


def describe(ticks: Iterable[Tick]) -> str:
    """One line for the plan: tick count and the largest tick."""
    ticks = list(ticks)
    largest = max((t.legs for t in ticks), default=0)
    return f"{len(ticks)} ticks, at most {largest} legs each"
