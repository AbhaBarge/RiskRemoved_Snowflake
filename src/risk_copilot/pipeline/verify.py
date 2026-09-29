"""``riskcopilot verify`` -- the post-load detection check (Phase 3, step 7).

Obligation 7. Offline, ``generate`` can show only that a cohort was *constructed*
to be detectable (it reports ``signal_yield`` as ``DEFERRED_TO_LOAD``). Whether
the real detection SQL fires is knowable only here, in Snowflake, after load.

Every judgement is made in SQL (``sql/verify/``), run as the admin role. This
module renders those queries, binds the configured values and reads back what
they return: it compares no count to a bound, reads no label and applies no
threshold.

1. **Refresh** the signal tables; the refresh cascades upstream. Verify never
   routes (D39): ``ROUTE_SIGNALS`` consumes ``SIGNALS_STREAM``, and a verify run
   between a replay and the routing task would swallow the change the task is
   meant to route.
2. **Signal yield.** Signals per registered rule must lie within
   ``validation.signal_yield``; a rule that fired nothing is listed as 0. It
   blocks (exit 1) when that gate's ``fail_build`` is set.
3. **Recall and precision** per rule, at party-month, against
   ``EVAL_GRAIN_PARTY_MONTH``, with the rule's false-positive control. A signal
   counts in the month of each of its evidence transactions (D40). Reported,
   not gated.
4. **The hero.** The pin ``generate`` wrote is checked against config offline
   and against the loaded seed. Then every ``must_trigger_rules`` entry must have
   fired for the pinned party. The result -- ``CONFIRMED_BY_DETECTION`` with the
   signal keys and case ids, or ``REJECTED_NOT_DETECTED`` (exit 1) -- goes to a
   sibling file (D41), so generate's pin stays byte-identical per seed.
5. **One ``GROUND_TRUTH.EVAL_RUN`` row** per run (D42).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from risk_copilot.config import ConfigError, Settings
from risk_copilot.generation.demo_entity import STATUS as PROVISIONAL
from risk_copilot.pipeline.deploy import Deployer, split_statements
from risk_copilot.session import execute

CONFIRMED = "CONFIRMED_BY_DETECTION"
REJECTED = "REJECTED_NOT_DETECTED"

#: ``EVAL_RUN.suite_name`` (the DDL's comment lists the suites).
SUITE = "DETECTION"

#: The queries under ``sql/verify/``, one statement each.
REFRESH = "01_refresh.sql"
SIGNAL_YIELD = "02_signal_yield.sql"
METRICS = "03_detection_metrics.sql"
LOADED_SEED = "04_loaded_seed.sql"
HERO = "05_hero.sql"
EVAL_RUN = "06_eval_run.sql"


class VerifyError(RuntimeError):
    """The pin, the config and the loaded data disagree, so nothing can be judged."""


# --------------------------------------------------------------------------- #
# The hero pin
# --------------------------------------------------------------------------- #


def pin_path(settings: Settings, key: str = "demo_entity_file") -> Path:
    """``output.<key>`` for this profile: the pin, or (``demo_entity_verified_file``) its result."""
    return settings.repo_root / str(settings.generation["output"][key]).format(
        profile=settings.profile
    )


def read_pin(settings: Settings) -> dict[str, Any]:
    """The pin ``generate`` wrote, refused unless it matches the configuration.

    Needs no connection, so a stale pin fails before anything is refreshed.
    """
    path = pin_path(settings)
    if not path.is_file():
        raise VerifyError(f"No hero pin at {path}; run `riskcopilot generate` first")
    pin = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    must = sorted(str(r) for r in settings.generation["demo_entity"]["must_trigger_rules"])
    expected = {
        "status": PROVISIONAL,
        "profile": settings.profile,
        "generation_seed": int(settings.generation["seed"]),
        "must_trigger_rules": must,
    }
    wrong = {k: pin.get(k) for k, v in expected.items() if pin.get(k) != v}
    if wrong or not pin.get("party_id") or not must:
        raise VerifyError(
            f"{path} does not match the configuration: {wrong or 'no party_id or rules'} "
            f"(expected {expected}). Regenerate before verifying."
        )
    return dict(pin)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuleYield:
    rule_id: str
    registered: bool
    signals: int
    #: Decided in SQL against the bound values.
    within_bounds: bool


@dataclass(frozen=True)
class HeroRule:
    rule_id: str
    detected: bool
    signal_business_keys: tuple[str, ...]
    #: Empty until ROUTE_SIGNALS has attached the signal to a case.
    case_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifyReport:
    refresh: list[dict[str, Any]]
    bounds: tuple[int, int]
    yield_blocks: bool
    yields: list[RuleYield]
    #: One row per registered rule, as ``03_detection_metrics.sql`` returns it.
    metrics: list[dict[str, Any]]
    party_id: str
    hero: list[HeroRule]
    status: str
    verified_path: Path
    run_id: str

    @property
    def yield_ok(self) -> bool:
        return bool(self.yields) and all(y.within_bounds for y in self.yields)

    @property
    def passed(self) -> bool:
        return (self.yield_ok or not self.yield_blocks) and self.status == CONFIRMED

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    @property
    def unrouted(self) -> list[str]:
        """Rules the hero fired whose signal is on no case yet (verify does not route)."""
        return [h.rule_id for h in self.hero if h.detected and not h.case_ids]


def verified_pin(pin: Mapping[str, Any], status: str, hero: Sequence[HeroRule]) -> dict[str, Any]:
    """The sibling file's content. No clock time: the same data gives the same bytes."""
    return {
        "status": status,
        "profile": pin["profile"],
        "generation_seed": pin["generation_seed"],
        "party_id": pin["party_id"],
        "must_trigger_rules": sorted(pin["must_trigger_rules"]),
        "rules": {
            h.rule_id: {
                "detected": h.detected,
                "signal_business_keys": list(h.signal_business_keys),
                "case_ids": list(h.case_ids),
            }
            for h in hero
        },
        "missing_rules": [h.rule_id for h in hero if not h.detected],
    }


def write_verified(path: Path, pin_file: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Written by `riskcopilot verify`: the post-load detection result for\n"
        f"# {pin_file.name}. Valid only while that pin names the same party_id and\n"
        "# generation_seed. No clock time is recorded, so the same data gives the same file.\n"
    )
    path.write_text(header + yaml.safe_dump(dict(doc), sort_keys=True), encoding="utf-8")


def git_commit(repo_root: Path) -> str | None:
    """HEAD, suffixed ``-dirty`` for uncommitted tracked changes; ``None`` without git."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # Fixed argument lists, no shell, no external input (S603).
        head = subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain", "--untracked-files=no"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{head}-dirty" if dirty else head


def _lower(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _array(value: Any) -> list[Any]:
    """An ARRAY column: Snowpark returns it as JSON text."""
    if value is None:
        return []
    if isinstance(value, str):
        return list(json.loads(value))
    return list(value)


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #


class Verifier:
    """Run the verify queries over an open session. See the module docstring."""

    def __init__(self, settings: Settings, session: Any) -> None:
        self.settings = settings
        self.session = session
        self.deployer = Deployer(settings)
        ctx = settings.sql_context()
        self.role = str(ctx["role"]["admin"])
        self.warehouse = str(ctx["wh"]["dev"])
        gate = settings.generation["validation"]["signal_yield"]
        self.bounds = (int(gate["min_detections_per_rule"]), int(gate["max_detections_per_rule"]))
        self.yield_blocks = bool(gate.get("fail_build", True))
        self.queries = {p.name: p for p in self.deployer.verify_files()}

    def statement(self, name: str) -> str:
        path = self.queries.get(name)
        if path is None:
            raise ConfigError(f"sql/verify/{name} is missing")
        statements = split_statements(self.deployer.render(path))
        if len(statements) != 1:
            raise ConfigError(f"sql/verify/{name} must hold one statement, not {len(statements)}")
        return statements[0]

    def _rows(self, name: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return [_lower(r.as_dict()) for r in execute(self.session, self.statement(name), params)]

    def run(self, pin: Mapping[str, Any]) -> VerifyReport:
        execute(self.session, f"USE ROLE {self.role}")
        execute(self.session, f"USE WAREHOUSE {self.warehouse}")
        started = execute(self.session, "SELECT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ")[0][0]

        seeds = self._rows(LOADED_SEED)
        loaded = [int(s) for s in _array(seeds[0]["seeds"])] if seeds else []
        if loaded != [int(pin["generation_seed"])]:
            raise VerifyError(
                f"The loaded injected legs carry generation seed(s) {loaded}, the pin "
                f"{pin['generation_seed']}: the data and the pin are from different builds. "
                "Reload the dataset the pin was generated with."
            )

        refresh = self._rows(REFRESH)
        yields = [
            RuleYield(
                rule_id=str(r["rule_id"]),
                registered=bool(r["registered"]),
                signals=int(r["signals"]),
                within_bounds=bool(r["within_bounds"]),
            )
            for r in self._rows(SIGNAL_YIELD, list(self.bounds))
        ]
        metrics = self._rows(METRICS)
        must = sorted(str(r) for r in pin["must_trigger_rules"])
        hero = [
            HeroRule(
                str(r["rule_id"]),
                bool(r["detected"]),
                tuple(str(k) for k in _array(r["signal_business_keys"])),
                tuple(str(c) for c in _array(r["case_ids"])),
            )
            for r in self._rows(HERO, [json.dumps(must), str(pin["party_id"])])
        ]
        status = CONFIRMED if hero and all(h.detected for h in hero) else REJECTED

        out = pin_path(self.settings, "demo_entity_verified_file")
        write_verified(out, pin_path(self.settings), verified_pin(pin, status, hero))
        report = VerifyReport(
            refresh=refresh,
            bounds=self.bounds,
            yield_blocks=self.yield_blocks,
            yields=yields,
            metrics=metrics,
            party_id=str(pin["party_id"]),
            hero=hero,
            status=status,
            verified_path=out,
            run_id=f"EVAL-{uuid.uuid4().hex[:16]}",
        )
        self._record(report, pin, started)
        return report

    def _record(self, report: VerifyReport, pin: Mapping[str, Any], started: Any) -> None:
        metrics = {
            "signal_yield": {
                "bounds": list(report.bounds),
                "blocking": report.yield_blocks,
                "rules": [asdict(y) for y in report.yields],
            },
            "detection": report.metrics,
            "hero": verified_pin(pin, report.status, report.hero),
            "refresh": report.refresh,
        }
        execute(
            self.session,
            self.statement(EVAL_RUN),
            [
                report.run_id,
                SUITE,
                self.settings.profile,
                git_commit(self.settings.repo_root),
                int(pin["generation_seed"]),
                json.dumps(metrics, sort_keys=True, default=str),
                report.passed,
                str(started),
            ],
        )


def verify(settings: Settings, session: Any, pin: Mapping[str, Any]) -> VerifyReport:
    """Verify detection for a checked ``pin`` (:func:`read_pin`) through ``session``."""
    return Verifier(settings, session).run(pin)
