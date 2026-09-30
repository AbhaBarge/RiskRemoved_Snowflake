"""``riskcopilot replay`` and ``load``'s hold-back (Phase 3, step 8), offline.

A real bundle is written with the generator's writer. The session is a small
simulator: it holds RAW_TRANSACTIONS, INJECTED_TXN_PENDING and the replay's
temporary table as sets of legs, applies the loader's and the replayer's
statements to them (BEGIN / ROLLBACK included), and answers their counts. That
is what lets a restart, a second run, and "hold-back + replay equals a full
load" be asserted without a connection.
"""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from risk_copilot.config import ConfigError, _replay_spec, duration_seconds, load_settings
from risk_copilot.generation.writer import write_dataset
from risk_copilot.pipeline.load import (
    Loader,
    LoadError,
    held_back_counts,
    promotion_sql,
    raw_fingerprint,
    read_bundle,
    window_filters,
)
from risk_copilot.pipeline.replay import (
    REPLAY_SOURCE,
    Leg,
    ReplayError,
    cohort_label,
    held_back_legs,
    inject,
    plan_ticks,
    replay,
)

pytestmark = pytest.mark.unit

D = dt.datetime
CUT = D(2022, 9, 3)  # conf/profiles/dev.yml replay.hold_back_from
HERO, OTHER = "P-HERO", "P-OTHER"
OWNER = {"A-HERO": HERO, "A-OTHER": OTHER}


# --------------------------------------------------------------------------- #
# A bundle split around the dev cut
# --------------------------------------------------------------------------- #


def _pair(pair: str, ts: D, debit: str, credit: str) -> list[dict[str, Any]]:
    return [
        {"txn_id": f"{pair}-D", "txn_pair_id": pair, "account_id": debit, "txn_timestamp": ts},
        {"txn_id": f"{pair}-C", "txn_pair_id": pair, "account_id": credit, "txn_timestamp": ts},
    ]


#: Source pairs: one loaded, one in tick 0 [00:00, 06:00), one in tick 2 [12:00, 18:00).
SOURCE = [
    *_pair("S1", D(2022, 9, 2, 10), "A-1", "A-2"),
    *_pair("S2", D(2022, 9, 3, 1), "A-1", "A-2"),
    *_pair("S3", D(2022, 9, 3, 13), "A-2", "A-1"),
]
#: Injected pairs: one loaded, the hero's in tick 1, another host's in tick 2.
PENDING = [
    *_pair("P1", D(2022, 9, 2, 20), "A-DESK", "A-HERO"),
    *_pair("P2", D(2022, 9, 3, 7), "A-DESK", "A-HERO"),
    *_pair("P3", D(2022, 9, 3, 13, 30), "A-DESK", "A-OTHER"),
]


def _table(rows: list[dict[str, Any]], **extra: pa.Array) -> pa.Table:
    cols: dict[str, Any] = {
        "txn_id": [r["txn_id"] for r in rows],
        "txn_pair_id": [r["txn_pair_id"] for r in rows],
        "account_id": [r["account_id"] for r in rows],
        "txn_timestamp": pa.array([r["txn_timestamp"] for r in rows], pa.timestamp("ms")),
    }
    return pa.table({**cols, **extra})


@pytest.fixture
def settings(repo_root: Path) -> Any:
    return load_settings(repo_root=repo_root, profile="dev")


@pytest.fixture
def label(settings: Any) -> str:
    return cohort_label(settings, "structuring_burst")


@pytest.fixture
def bundle_dir(tmp_path: Path, settings: Any, label: str) -> Path:
    n = len(PENDING)
    pending = _table(
        PENDING,
        ctr_clause_id=pa.array(["CTR_CASH_THRESHOLD"] * n),
        currency=pa.array(["INR"] * n),
        amount_ctr_ratio=pa.array([Decimal("0.95")] * n, pa.decimal128(12, 10)),
        cohort=pa.array([label] * n),
    )
    labels = pa.table({"txn_id": [r["txn_id"] for r in SOURCE + PENDING]})
    write_dataset(
        tmp_path,
        {"RAW_TRANSACTIONS": _table(SOURCE)},
        {"INJECTED_TXN_PENDING": pending, "TXN_LABELS": labels},
        {"passed": True, "profile": "dev", "gates": []},
        settings.generation["output"],
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# The simulator
# --------------------------------------------------------------------------- #


class Row(tuple):  # type: ignore[type-arg]
    def as_dict(self) -> dict[str, Any]:
        return dict(self[0]) if self and isinstance(self[0], dict) else {}


_TEXT = ("TEXT", None, None)
_TS = ("TIMESTAMP_NTZ", None, None)
COLUMNS = {
    "RAW": [
        ("RAW_TRANSACTIONS", c, *t)
        for c, t in (
            ("TXN_ID", _TEXT),
            ("TXN_PAIR_ID", _TEXT),
            ("ACCOUNT_ID", _TEXT),
            ("TXN_TIMESTAMP", _TS),
        )
    ],
    "GROUND_TRUTH": [
        *[
            ("INJECTED_TXN_PENDING", c, *t)
            for c, t in (
                ("TXN_ID", _TEXT),
                ("TXN_PAIR_ID", _TEXT),
                ("ACCOUNT_ID", _TEXT),
                ("TXN_TIMESTAMP", _TS),
                ("CTR_CLAUSE_ID", _TEXT),
                ("CURRENCY", _TEXT),
                ("AMOUNT_CTR_RATIO", ("NUMBER", 12, 10)),
                ("COHORT", _TEXT),
            )
        ],
        ("TXN_LABELS", "TXN_ID", *_TEXT),
    ],
}

#: Pending predicates the loader and replayer render, in the order their
#: parameters are bound.
_PREDICATES = {
    r"txn_timestamp >= \?": lambda row, v: row["ts"] >= D.fromisoformat(v),
    r"txn_timestamp < \?": lambda row, v: row["ts"] < D.fromisoformat(v),
    r"cohort = \?": lambda row, v: row["cohort"] == v,
    r"party_id = \?": lambda row, v: v in row["parties"],
}


class Sim:
    """A recording session that models the three tables replay touches."""

    def __init__(self, bundle_dir: Path) -> None:
        self.dir = bundle_dir
        self.raw: dict[str, D] = {}
        self.temp: dict[str, D] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.statements: list[tuple[str, list[Any] | None]] = []
        self.puts: list[tuple[str, str]] = []
        self.file = SimpleNamespace(put=self._put)
        self.saved: Any = None
        #: Raise on the n-th replay source INSERT (1-based), to simulate a crash.
        self.fail_on_source_insert: int | None = None
        self.source_inserts = 0
        #: Rows the next DELETE reports, to simulate a load that deleted the wrong rows.
        self.delete_reports: int | None = None
        self.labels = 0

    # ---- plumbing ------------------------------------------------------- #

    def _put(self, local: str, stage: str, *, auto_compress: bool, overwrite: bool) -> Any:
        self.puts.append((Path(local).name, stage))
        return [SimpleNamespace(target=Path(local).name)]

    def _file(self, zone: str, table: str) -> list[dict[str, Any]]:
        return pq.read_table(self.dir / zone / f"{table}.parquet").to_pylist()

    @property
    def sent(self) -> list[str]:
        return [s for s, _ in self.statements]

    def _select(self, sql: str, params: list[Any], *, unresolved: bool = True) -> list[str]:
        found = sorted(
            (m.start(), fn) for p, fn in _PREDICATES.items() for m in re.finditer(p, sql)
        )
        tests = list(zip((fn for _, fn in found), params, strict=True))
        return [
            txn_id
            for txn_id, row in self.pending.items()
            if (not unresolved or not row["resolved"]) and all(fn(row, v) for fn, v in tests)
        ]

    # ---- the statements ---------------------------------------------------- #

    def sql(self, sql: str, params: list[Any] | None = None) -> Any:
        self.statements.append((sql, params))
        rows = self._answer(sql, list(params or []))
        return SimpleNamespace(collect=lambda: [Row(r) for r in rows])

    def _answer(self, sql: str, params: list[Any]) -> list[Any]:
        head = " ".join(sql.split())
        if "INFORMATION_SCHEMA.COLUMNS" in head:
            if "column_name <> ?" in head:  # raw_fingerprint
                return [(c[1],) for c in COLUMNS["RAW"] if c[1] != params[2]]
            return COLUMNS[params[0]]
        if head == "BEGIN":
            self.saved = copy.deepcopy((self.raw, self.pending))
            return []
        if head == "ROLLBACK":
            self.raw, self.pending = self.saved
            return []
        if head == "COMMIT":
            return []
        if "V_POLICY_CLAUSE_CURRENT WHERE clause_id = ?" in head:
            return [(1,)]
        if head.startswith("SHOW GRANTS ON STAGE"):
            return [({"privilege": "OWNERSHIP", "grantee_name": "RC_ADMIN_DEV"},)]
        if head.startswith("CREATE OR REPLACE TEMPORARY TABLE"):
            self.temp = {}
            return []
        if head.startswith("COPY INTO"):
            target = head.split()[2]
            if target.endswith(REPLAY_SOURCE):
                self.temp = {
                    r["txn_id"]: r["txn_timestamp"] for r in self._file("raw", "RAW_TRANSACTIONS")
                }
            elif target.endswith("RAW_TRANSACTIONS"):
                self.raw.update(
                    {r["txn_id"]: r["txn_timestamp"] for r in self._file("raw", "RAW_TRANSACTIONS")}
                )
            elif target.endswith("INJECTED_TXN_PENDING"):
                rows = self._file("ground_truth", "INJECTED_TXN_PENDING")
                pairs: dict[str, set[str]] = {}
                for r in rows:
                    pairs.setdefault(r["txn_pair_id"], set()).add(OWNER.get(r["account_id"], ""))
                for r in rows:
                    self.pending[r["txn_id"]] = {
                        "ts": r["txn_timestamp"],
                        "cohort": r["cohort"],
                        "parties": pairs[r["txn_pair_id"]],
                        "resolved": False,
                    }
            elif target.endswith("TXN_LABELS"):
                self.labels = len(self._file("ground_truth", "TXN_LABELS"))
            return []
        if head.startswith("DELETE FROM") and "txn_timestamp >= ?" in head:
            cut = D.fromisoformat(params[0])
            held = [i for i, ts in self.raw.items() if ts >= cut]
            for i in held:
                del self.raw[i]
            return [(self.delete_reports if self.delete_reports is not None else len(held),)]
        if head.startswith("INSERT INTO") and f"{REPLAY_SOURCE} s" in head:
            self.source_inserts += 1
            if self.source_inserts == self.fail_on_source_insert:
                raise RuntimeError("simulated crash")
            lo, hi = (D.fromisoformat(v) for v in params)
            new = [i for i, ts in self.temp.items() if lo <= ts < hi and i not in self.raw]
            self.raw.update({i: self.temp[i] for i in new})
            return [(len(new),)]
        if head.startswith("INSERT INTO") and "INJECTED_TXN_PENDING p" in head:
            ids = [i for i in self._select(head, params[2:]) if i not in self.raw]
            self.raw.update({i: self.pending[i]["ts"] for i in ids})
            return [(len(ids),)]
        if head.startswith("UPDATE") and "INJECTED_TXN_PENDING" in head:
            for i in self._select(head, params):
                self.pending[i]["resolved"] = True
            return []
        if head.startswith("SELECT COUNT(*), COUNT_IF(resolved_at IS NULL)"):  # inject summary
            ids = self._select(head, params, unresolved=False)
            waiting = [self.pending[i]["ts"] for i in ids if not self.pending[i]["resolved"]]
            return [
                (len(ids), len(waiting), min(waiting, default=None), max(waiting, default=None))
            ]
        if head.startswith("SELECT COUNT(*), HASH_AGG("):
            return [(len(self.raw), hash(frozenset(self.raw.items())))]
        if head.startswith("SELECT txn_id FROM"):
            cut = D.fromisoformat(params[0])
            return [(i,) for i, ts in self.raw.items() if ts >= cut]
        if "COUNT(DISTINCT txn_id)" in head:
            return [(0,)]  # a dict cannot hold a leg twice; the anti-join tests cover it
        if f"FROM {self._fq_source(head)} r WHERE NOT EXISTS" in head:  # foreign rows
            return [(sum(1 for i in self.raw if i not in self.temp and i not in self.pending),)]
        if re.search(rf"FROM \S+{REPLAY_SOURCE} s WHERE s\.txn_timestamp < \?", head):
            cut = D.fromisoformat(params[0])
            return [(sum(1 for i, ts in self.temp.items() if ts < cut and i not in self.raw),)]
        if head.startswith("SELECT COUNT(*) FROM") and "INJECTED_TXN_PENDING" in head:
            if "LEFT JOIN" in head:
                return [(0,)]
            if "WHERE resolved_at IS NULL" in head:
                return [(len(self._select(head, params)),)]
            return [(len(self.pending),)]
        if head.startswith("SELECT COUNT(*) FROM") and "RAW_TRANSACTIONS" in head:
            if "LEFT JOIN" in head:
                return [(0,)]
            if "txn_timestamp >= ?" in head:
                cut = D.fromisoformat(params[0])
                return [(sum(1 for ts in self.raw.values() if ts >= cut),)]
            return [(len(self.raw),)]
        if head.startswith("SELECT COUNT(*) FROM") and head.endswith(REPLAY_SOURCE):
            return [(len(self.temp),)]
        if head.startswith("SELECT COUNT(*) FROM") and head.endswith("TXN_LABELS"):
            return [(self.labels,)]
        raise AssertionError(f"The simulator does not know this statement: {head[:160]}")

    @staticmethod
    def _fq_source(head: str) -> str:
        m = re.search(r"FROM (\S+RAW_TRANSACTIONS) r WHERE NOT EXISTS", head)
        return m.group(1) if m else "\x00"


def hold_back_load(settings: Any, sim: Sim, bundle_dir: Path) -> Any:
    return Loader(settings, sim, source=False, hold_back=settings.replay.hold_back_from).run(
        read_bundle(bundle_dir)
    )


def no_sleep(_: float) -> None:
    pass


class Sleeps(list):  # type: ignore[type-arg]
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


ALL_IDS = {r["txn_id"] for r in SOURCE + PENDING}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_the_replay_block_resolves_for_both_profiles(repo_root: Path, settings: Any) -> None:
    dev, demo = settings.replay, load_settings(repo_root=repo_root, profile="demo").replay
    assert dev.hold_back_from == CUT and demo.hold_back_from is None
    for spec in (dev, demo):
        assert spec.sim_step_seconds == duration_seconds("6 hours")
        assert spec.batch_legs == settings.generation["replay"]["batch_legs"]
        assert spec.injections == {"structuring_burst": "structuring"}
    assert (
        cohort_label(settings, "structuring_burst")
        == (settings.generation["injection"]["structuring"]["label"])
    )


def test_a_yaml_date_holds_back_from_its_midnight(settings: Any) -> None:
    gen = copy.deepcopy(settings.generation)
    gen["replay"]["hold_back_from"] = dt.date(2022, 9, 3)
    assert _replay_spec(gen).hold_back_from == CUT


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("sim_step", "6 fortnights", "Invalid duration"),
        ("sim_step", "0 hours", "positive"),
        ("batch_legs", 0, "batch_legs"),
        ("batch_legs", "many", "replay"),
        ("sleep_seconds", -1, "sleep_seconds"),
        ("injections", {"structuring_burst": "no_such_cohort"}, "no_such_cohort"),
        ("hold_back_from", D(2022, 9, 3, tzinfo=dt.UTC), "naive"),
    ],
)
def test_a_bad_replay_block_is_refused(settings: Any, key: str, value: Any, match: str) -> None:
    gen = copy.deepcopy(settings.generation)
    gen["replay"][key] = value
    with pytest.raises(ConfigError, match=match):
        _replay_spec(gen)


def test_a_missing_replay_block_is_refused(settings: Any) -> None:
    gen = {k: v for k, v in settings.generation.items() if k != "replay"}
    with pytest.raises(ConfigError, match="no `replay` block"):
        _replay_spec(gen)


def test_durations_use_the_target_lag_grammar() -> None:
    assert duration_seconds("6 hours") == duration_seconds("360 minutes") == 21600
    assert duration_seconds("1 day") == 86400
    with pytest.raises(ConfigError):
        duration_seconds("DOWNSTREAM")


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


def _legs(*stamps: D) -> list[Leg]:
    return [Leg(ts, f"T{n:03d}", True) for n, ts in enumerate(stamps)]


def test_ticks_follow_the_sim_step_grid(settings: Any) -> None:
    legs = _legs(D(2022, 9, 3, 1), D(2022, 9, 3, 13), D(2022, 9, 3, 13))
    ticks = plan_ticks(legs, CUT, 6 * 3600, 500)
    assert [(t.start.hour, t.end.hour, t.legs) for t in ticks] == [
        (0, 6, 1),
        (6, 12, 0),
        (12, 18, 2),
    ]


def test_a_full_step_is_cut_early_at_a_timestamp_boundary() -> None:
    at = [D(2022, 9, 3, 1)] * 2 + [D(2022, 9, 3, 2)] * 2 + [D(2022, 9, 3, 3)] * 2
    ticks = plan_ticks(_legs(*at), CUT, 6 * 3600, 5)
    assert [t.legs for t in ticks] == [4, 2]
    assert ticks[0].end == D(2022, 9, 3, 3) == ticks[1].start
    # The cut tick resumes the same step: the grid is not shifted.
    assert ticks[1].end == D(2022, 9, 3, 6)


def test_one_timestamp_over_the_cap_is_taken_whole() -> None:
    """Splitting a timestamp would split a pair (M36: no pair spans two)."""
    at = [D(2022, 9, 3, 1)] * 4 + [D(2022, 9, 3, 2)]
    ticks = plan_ticks(_legs(*at), CUT, 6 * 3600, 3)
    assert [t.legs for t in ticks] == [4, 1]


def test_every_leg_lands_in_exactly_one_tick() -> None:
    at = [CUT + dt.timedelta(minutes=37 * n) for n in range(400)]
    legs = _legs(*at)
    ticks = plan_ticks(legs, CUT, 6 * 3600, 7)
    ids = [i for t in ticks for i in t.source_ids]
    assert sorted(ids) == sorted(leg.txn_id for leg in legs)
    assert all(a.end == b.start for a, b in itertools.pairwise(ticks))
    assert max(t.legs for t in ticks) <= 7
    assert ticks == plan_ticks(legs, CUT, 6 * 3600, 7)


def test_the_plan_reads_both_files_in_timestamp_order(bundle_dir: Path) -> None:
    legs = held_back_legs(read_bundle(bundle_dir), CUT)
    assert [leg.txn_id for leg in legs] == [
        "S2-C", "S2-D", "P2-C", "P2-D", "S3-C", "S3-D", "P3-C", "P3-D"
    ]  # fmt: skip
    assert [leg.source for leg in legs[:4]] == [True, True, False, False]
    assert held_back_counts(read_bundle(bundle_dir), CUT) == (4, 4)


# --------------------------------------------------------------------------- #
# load's hold-back
# --------------------------------------------------------------------------- #


def test_load_holds_back_the_legs_at_or_after_the_cut(
    settings: Any, bundle_dir: Path, label: str
) -> None:
    sim = Sim(bundle_dir)
    report = hold_back_load(settings, sim, bundle_dir)
    assert report.ok and report.held_back_from == CUT
    assert set(sim.raw) == {"S1-D", "S1-C", "P1-D", "P1-C"}
    assert {i for i, r in sim.pending.items() if not r["resolved"]} == {
        "P2-D", "P2-C", "P3-D", "P3-C"
    }  # fmt: skip
    sent = sim.sent
    copy_tx = next(
        i for i, s in enumerate(sent) if s.startswith("COPY INTO") and "RAW_TRANSACTIONS" in s
    )
    delete = next(i for i, s in enumerate(sent) if s.startswith("DELETE FROM"))
    promote = next(i for i, s in enumerate(sent) if "INJECTED_TXN_PENDING p" in s)
    assert sent.index("BEGIN") < copy_tx < delete < promote < sent.index("COMMIT")
    assert sim.statements[delete][1] == [str(CUT)]
    assert "AND p.txn_timestamp < ?::TIMESTAMP_NTZ" in sent[promote]
    held = next(c for c in report.checks if c.name == "source legs held back for replay")
    assert held.ok and held.value == "4"


def test_a_hold_back_that_deletes_the_wrong_rows_rolls_back(
    settings: Any, bundle_dir: Path
) -> None:
    sim = Sim(bundle_dir)
    sim.delete_reports = 3
    with pytest.raises(LoadError, match="source legs held back"):
        hold_back_load(settings, sim, bundle_dir)
    assert "ROLLBACK" in sim.sent and "COMMIT" not in sim.sent


def test_promotion_is_anti_joined_and_carries_its_filters() -> None:
    filters = window_filters(CUT, CUT + dt.timedelta(hours=6))
    sql = promotion_sql("DB.RAW.T", "DB.GT.P", "DB.GOV.V", [f.render("p.") for f in filters])
    assert "WHERE p.resolved_at IS NULL\n  AND p.txn_timestamp >= ?::TIMESTAMP_NTZ\n" in sql
    assert "AND p.txn_timestamp < ?::TIMESTAMP_NTZ\n" in sql
    assert sql.endswith("AND NOT EXISTS (SELECT 1 FROM DB.RAW.T r WHERE r.txn_id = p.txn_id)")
    assert [f.params for f in filters] == [(str(CUT),), (str(CUT + dt.timedelta(hours=6)),)]
    assert not re.search(r"\d{4,}", sql)


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


def test_replay_streams_every_held_back_leg(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    sleeps = Sleeps()
    report = replay(settings, sim, read_bundle(bundle_dir), sleep=sleeps)
    assert report.ok, report.checks
    assert [(r.source, r.promoted, r.skipped) for r in report.results] == [
        (2, 0, False), (0, 2, False), (2, 2, False)
    ]  # fmt: skip
    assert set(sim.raw) == ALL_IDS
    assert not any(not r["resolved"] for r in sim.pending.values())
    # One sleep between executed ticks, of the configured length.
    assert sleeps == [settings.replay.sleep_seconds] * 2
    begins = [i for i, s in enumerate(sim.sent) if s == "BEGIN"]
    assert len(begins) == 1 + 3  # the load, then one transaction per tick


def test_hold_back_plus_replay_equals_a_full_load(settings: Any, bundle_dir: Path) -> None:
    """The equivalence check the live proof repeats with HASH_AGG."""
    full = Sim(bundle_dir)
    Loader(settings, full, source=False).run(read_bundle(bundle_dir))
    split = Sim(bundle_dir)
    hold_back_load(settings, split, bundle_dir)
    replay(settings, split, read_bundle(bundle_dir), sleep=no_sleep)
    assert split.raw == full.raw
    fq_raw = settings.sql_context()["fq"]["raw"]
    assert raw_fingerprint(split, fq_raw) == raw_fingerprint(full, fq_raw)


def test_a_restart_skips_finished_ticks_and_inserts_nothing_twice(
    settings: Any, bundle_dir: Path
) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    sim.fail_on_source_insert = 2  # tick 1 (the hero's): its INSERT fails
    with pytest.raises(Exception, match="simulated crash"):
        replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    assert sim.sent[-1] == "ROLLBACK"
    assert set(sim.raw) == {"S1-D", "S1-C", "P1-D", "P1-C", "S2-D", "S2-C"}
    sim.fail_on_source_insert = None
    report = replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    assert [r.skipped for r in report.results] == [True, False, False]
    assert report.ok and set(sim.raw) == ALL_IDS


def test_a_failed_rollback_does_not_hide_the_original_error(
    settings: Any, bundle_dir: Path
) -> None:
    """Measured: the machine slept mid-replay; the expired session failed the ROLLBACK too."""
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    sim.fail_on_source_insert = 1
    original = sim._answer

    def lost_session(sql: str, params: list[Any]) -> list[Any]:
        if sql == "ROLLBACK":
            raise RuntimeError("390114: Authentication token has expired")
        return original(sql, params)

    sim._answer = lost_session  # type: ignore[method-assign]
    with pytest.raises(ReplayError, match=r"(?s)simulated crash.*ROLLBACK also failed.*390114"):
        replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)


def test_a_second_replay_changes_nothing(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    before, n = dict(sim.raw), len(sim.statements)
    sleeps = Sleeps()
    report = replay(settings, sim, read_bundle(bundle_dir), sleep=sleeps)
    assert report.ok and all(r.skipped for r in report.results)
    assert sim.raw == before and sleeps == []
    assert "BEGIN" not in sim.sent[n:]


def test_replay_refuses_rows_this_bundle_did_not_write(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    sim.raw["T-FOREIGN"] = D(2022, 9, 2)
    with pytest.raises(ReplayError, match="another build"):
        replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    assert "BEGIN" not in sim.sent[sim.sent.index("COMMIT") + 1 :]


def test_replay_refuses_a_load_that_held_back_more(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    Loader(settings, sim, source=False, hold_back=D(2022, 9, 2)).run(read_bundle(bundle_dir))
    with pytest.raises(ReplayError, match="later hold-back, or not at all"):
        replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)


def test_replay_needs_a_hold_back(repo_root: Path, bundle_dir: Path) -> None:
    demo = load_settings(repo_root=repo_root, profile="demo")
    with pytest.raises(ReplayError, match="nothing to replay"):
        replay(demo, Sim(bundle_dir), read_bundle(bundle_dir), sleep=no_sleep)


def test_replay_reads_the_bundle_file_through_a_temporary_table(
    settings: Any, bundle_dir: Path
) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    n_puts, n = len(sim.puts), len(sim.statements)
    replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    ctx = settings.sql_context()
    assert sim.puts[n_puts:] == [
        (
            "RAW_TRANSACTIONS.parquet",
            f"@{ctx['fq']['raw']}.{ctx['stage']['generated']}/dev/"
            f"{read_bundle(bundle_dir).run_key}/raw",
        )
    ]
    sent = sim.sent[n:]
    create = next(s for s in sent if "TEMPORARY TABLE" in s)
    assert create.startswith(
        f"CREATE OR REPLACE TEMPORARY TABLE {ctx['fq']['raw']}.{REPLAY_SOURCE}"
    )
    assert create.endswith("LIMIT 0")
    (copy_sql,) = [s for s in sent if s.startswith("COPY INTO")]
    assert f"{REPLAY_SOURCE} (TXN_ID, TXN_PAIR_ID, ACCOUNT_ID, TXN_TIMESTAMP)" in copy_sql
    assert 'TO_TIMESTAMP_NTZ($1:"txn_timestamp"::NUMBER, 3)' in copy_sql
    assert "FORCE = TRUE" in copy_sql


def test_replay_leaves_ingested_at_to_its_default(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    inserts = [s for s in sim.sent if s.startswith("INSERT INTO")]
    assert inserts and not any("INGESTED_AT" in s.upper() for s in inserts)


def test_replay_never_refreshes_or_routes(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    n = len(sim.statements)
    replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    st = settings.sql_context()["stream_task"]
    for s in sim.sent[n:]:
        up = s.upper()
        assert not re.search(r"\b(CALL|EXECUTE TASK|RESUME|ALTER DYNAMIC TABLE|MERGE)\b", up)
        assert st["signals_stream"] not in up and st["route_signals_task"] not in up


def test_replay_statements_bind_every_value(settings: Any, bundle_dir: Path) -> None:
    """No timestamp, id or threshold is rendered into a statement; each is bound."""
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    n = len(sim.statements)
    replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    for s in sim.sent[n:]:
        if s.startswith("COPY INTO"):  # a content-addressed stage path
            continue
        assert not re.search(r"\d{4,}|'\d", s), s


def test_a_tick_that_inserts_other_than_planned_rolls_back(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    original = sim._answer

    def lying(sql: str, params: list[Any]) -> list[Any]:
        rows = original(sql, params)
        if f"{REPLAY_SOURCE} s" in sql and sql.startswith("INSERT INTO"):
            return [(rows[0][0] + 1,)]
        return rows

    sim._answer = lying  # type: ignore[method-assign]
    with pytest.raises(ReplayError, match="planned"):
        replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    assert sim.sent[-1] == "ROLLBACK"


# --------------------------------------------------------------------------- #
# --inject
# --------------------------------------------------------------------------- #


def test_inject_releases_one_hosts_burst(settings: Any, bundle_dir: Path, label: str) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    n = len(sim.statements)
    done = inject(settings, sim, "structuring_burst", HERO)
    assert (done.promoted, done.party_id, done.cohort) == (2, HERO, label)
    assert done.first == done.last == D(2022, 9, 3, 7)
    assert {"P2-D", "P2-C"} <= set(sim.raw) and "P3-C" not in sim.raw  # the other host waits
    sent = sim.statements[n:]
    promote_sql, params = next((s, p) for s, p in sent if "INJECTED_TXN_PENDING p" in s)
    ctx = settings.sql_context()["fq"]
    assert f"JOIN {ctx['raw']}.RAW_ACCOUNTS a ON a.account_id = q.account_id" in promote_sql
    assert "p.txn_pair_id IN (SELECT q.txn_pair_id" in promote_sql
    assert params is not None and params[2:] == [label, HERO]
    assert [s for s, _ in sent].count("BEGIN") == 1 and sent[-1][0] == "COMMIT"


def test_inject_then_replay_inserts_nothing_twice(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    inject(settings, sim, "structuring_burst", HERO)
    report = replay(settings, sim, read_bundle(bundle_dir), sleep=no_sleep)
    assert report.ok and set(sim.raw) == ALL_IDS
    assert [(r.source, r.promoted, r.skipped) for r in report.results] == [
        (2, 0, False), (0, 0, True), (2, 2, False)
    ]  # fmt: skip


def test_inject_refuses_a_non_host_and_a_released_burst(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    hold_back_load(settings, sim, bundle_dir)
    with pytest.raises(ReplayError, match="not a host"):
        inject(settings, sim, "structuring_burst", "P-NOBODY")
    inject(settings, sim, "structuring_burst", HERO)
    n = len(sim.statements)
    with pytest.raises(ReplayError, match="released earlier"):
        inject(settings, sim, "structuring_burst", HERO)
    assert "BEGIN" not in sim.sent[n:]


def test_an_unknown_inject_name_is_refused(settings: Any, bundle_dir: Path) -> None:
    with pytest.raises(ReplayError, match=r"Unknown --inject 'mule'.*structuring_burst"):
        inject(settings, Sim(bundle_dir), "mule", HERO)


# --------------------------------------------------------------------------- #
# The fingerprint and the CLI
# --------------------------------------------------------------------------- #


def test_the_fingerprint_leaves_out_ingested_at(settings: Any, bundle_dir: Path) -> None:
    sim = Sim(bundle_dir)
    raw_fingerprint(sim, settings.sql_context()["fq"]["raw"])
    (cols_sql, params), (hash_sql, _) = sim.statements
    assert params is not None and params[1:] == ["RAW_TRANSACTIONS", "INGESTED_AT"]
    assert "ORDER BY ordinal_position" in cols_sql
    assert "HASH_AGG(TXN_ID, TXN_PAIR_ID, ACCOUNT_ID, TXN_TIMESTAMP)" in hash_sql


def _cli(*args: str) -> Any:
    from typer.testing import CliRunner

    from risk_copilot import cli

    return CliRunner().invoke(cli.app, list(args))


def _no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from risk_copilot import session

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("replay opened a session it should have refused before connecting")

    monkeypatch.setattr(session, "open_session", refuse)


def test_cli_party_needs_inject(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_session(monkeypatch)
    result = _cli("--profile", "dev", "replay", "--party", HERO)
    assert result.exit_code == 2 and "--party needs --inject" in result.output


def test_cli_refuses_a_profile_with_nothing_held_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_session(monkeypatch)
    result = _cli("--profile", "demo", "replay")
    assert result.exit_code == 2 and "Nothing to replay" in result.output
