"""``riskcopilot`` -- the single entry point for every operation.

No part of this project is invoked by running a loose script: every operation
is a subcommand, so the profile, logging and configuration resolution are
identical no matter who runs it.

Subcommands:

* ``doctor``    verify config, datasets, connection, edition, ML functions, models
* ``config``    show the resolved configuration for a profile
* ``deploy``    render and apply the SQL stages, idempotently
* ``generate``  build the synthetic dataset and run the validation gates
* ``load``      stage and COPY INTO the generated data
* ``verify``    refresh detection and check it against the generated truth
* ``replay``    drive the replay harness
* ``app``       render, run or deploy the Streamlit-in-Snowflake app
* ``eval``      run the evaluation suites
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from risk_copilot import __version__
from risk_copilot import doctor as doctor_mod
from risk_copilot.config import ConfigError, Settings, load_settings
from risk_copilot.doctor import Status

app = typer.Typer(
    name="riskcopilot",
    help="Risk & Compliance Copilot -- Snowflake-native AML signal-to-STR pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_state: dict[str, Any] = {"profile": None, "repo_root": None}

_STATUS_STYLE = {
    Status.OK: "[green]OK  [/green]",
    Status.WARN: "[yellow]WARN[/yellow]",
    Status.FAIL: "[red]FAIL[/red]",
    Status.SKIP: "[dim]SKIP[/dim]",
}


def _load_dotenv(start: Path | None = None) -> Path | None:
    """Load ``.env`` into the environment, returning the file used.

    ``.env`` holds pointers -- which connection to use, where the private key
    lives -- and every consumer reads them via :mod:`os.environ`. Loading it
    here means one entry point applies it, so a developer does not have to
    export the same variables by hand in every shell. Values already present in
    the environment win, so an explicit ``RC_*`` override on the command line is
    never clobbered by the file. Parsed with the standard library rather than
    adding a dependency for eight lines of work.
    """
    origin = (start or Path.cwd()).resolve()
    for directory in (origin, *origin.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            break
    else:
        return None

    for raw in candidate.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return candidate


@app.callback()
def main(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Config profile: dev | demo. Default: $RC_PROFILE."),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", help="Repository root. Defaults to the installed location."),
    ] = None,
    log_level: Annotated[
        str, typer.Option("--log-level", help="DEBUG | INFO | WARNING | ERROR.")
    ] = "INFO",
) -> None:
    """Global options, applied to every subcommand."""
    _load_dotenv(repo_root)
    _state.update(profile=profile, repo_root=repo_root)
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(levelname)-8s %(name)s: %(message)s",
    )


def _settings() -> Settings:
    """Load settings, turning configuration errors into clean CLI failures."""
    try:
        return load_settings(repo_root=_state["repo_root"], profile=_state["profile"])
    except ConfigError as exc:
        console.print(f"[red]Configuration error[/red]\n{exc}")
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"risk-copilot {__version__}")


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@app.command()
def doctor(
    data: Annotated[bool, typer.Option("--data/--no-data", help="Verify source datasets.")] = True,
    connection: Annotated[
        bool, typer.Option("--connection/--no-connection", help="Verify the Snowflake connection.")
    ] = True,
    models: Annotated[
        bool,
        typer.Option("--models", help="Probe each agent model. Costs a few tokens per candidate."),
    ] = False,
    checksums: Annotated[
        bool,
        typer.Option("--checksums", help="Verify dataset SHA-256 hashes. Slow (~950 MB)."),
    ] = False,
    features: Annotated[
        bool,
        typer.Option(
            "--features/--no-features",
            help="Resolve which build features this account supports, from "
            "conf/generated/capabilities.<connection>.yml.",
        ),
    ] = True,
) -> None:
    """Diagnose the environment before generating, deploying or demoing.

    Exits non-zero if any check fails, so it can gate a CI job or a deploy.
    """
    settings = _settings()
    report = doctor_mod.run(
        settings,
        include_data=data,
        include_connection=connection,
        include_models=models,
        include_features=features,
        verify_checksums=checksums,
    )

    current_group = None
    for check in report.checks:
        if check.group != current_group:
            current_group = check.group
            console.print(f"\n[bold]{current_group}[/bold]")
        console.print(f"  {_STATUS_STYLE[check.status]}  {check.name}")
        if check.detail:
            for line in check.detail.splitlines():
                console.print(f"          [dim]{line}[/dim]")
        if check.remedy:
            for line in check.remedy.splitlines():
                console.print(f"          [yellow]-> {line}[/yellow]")

    total = len(report.checks)
    console.print(
        f"\n{total} checks: "
        f"[red]{len(report.failed)} failed[/red], "
        f"[yellow]{len(report.warned)} warnings[/yellow]"
    )
    raise typer.Exit(code=report.exit_code)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@app.command("config")
def show_config(
    section: Annotated[
        str,
        typer.Option(
            "--section",
            "-s",
            help="topology | generation | detection | models | external | ml | sql",
        ),
    ] = "topology",
) -> None:
    """Show the resolved configuration after profile and environment layering."""
    settings = _settings()

    if section == "topology":
        table = Table(title=f"Resolved topology -- profile '{settings.profile}'")
        table.add_column("Setting", style="cyan", no_wrap=True)
        table.add_column("Value")

        table.add_row("profile", settings.profile)
        table.add_row("object suffix", settings.object_suffix or "(none)")
        table.add_row("database", settings.db())
        for key in ("raw", "curated", "governance", "evidence", "audit", "ground_truth", "ml"):
            table.add_row(f"schema.{key}", settings.fq(key))
        for key, wh in settings.warehouses.items():
            table.add_row(f"warehouse.{key}", f"{settings.obj(wh.name)} ({wh.size})")
        table.add_row("role.admin", settings.obj(settings.roles.admin))
        table.add_row("role.service", settings.obj(settings.roles.service))
        table.add_row("role.compliance", settings.obj(settings.roles.compliance_officer))
        for region, role in settings.roles.branch_analysts.items():
            table.add_row(f"role.analyst.{region}", settings.obj(role))
        for key, spec in settings.dynamic_tables.items():
            table.add_row(f"dt.{key}", f"{settings.obj(spec.name)} lag={spec.target_lag}")
        table.add_row("cortex.semantic_view", settings.obj(settings.cortex.semantic_view))
        table.add_row("cortex.search_service", settings.obj(settings.cortex.search_service))
        table.add_row("cortex.agent", settings.obj(settings.cortex.agent))
        table.add_row("connection", settings.connection_name)
        console.print(table)
        return

    payloads = {
        "generation": settings.generation,
        "detection": settings.detection,
        "models": settings.models,
        "external": settings.external,
        "ml": settings.ml,
        "sql": settings.sql_context(),
    }
    if section not in payloads:
        console.print(
            f"[red]Unknown section {section!r}[/red]; expected one of {['topology', *payloads]}"
        )
        raise typer.Exit(code=2)
    console.print_json(json.dumps(payloads[section], indent=2, default=str))


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #


@app.command()
def deploy(
    stage: Annotated[
        list[str] | None,
        typer.Option(
            "--stage",
            "-s",
            help="Stages to apply, in order. Repeatable. Default: all. "
            "e.g. bootstrap, ddl, dynamic_tables, streams_tasks, governance",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render SQL and print it without executing."),
    ] = False,
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Write rendered SQL here (implies --dry-run)."),
    ] = None,
) -> None:
    """Render and apply the SQL stages.

    Every statement is idempotent (``CREATE OR ALTER`` / ``IF NOT EXISTS``), so
    re-running a stage is safe and is the normal way to apply a change.
    """
    from risk_copilot.pipeline.deploy import Deployer

    settings = _settings()
    deployer = Deployer(settings)
    try:
        result = deployer.run(stages=stage, dry_run=dry_run or out_dir is not None, out_dir=out_dir)
    except (ConfigError, FileNotFoundError) as exc:
        console.print(f"[red]Deploy failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    table = Table(
        title=f"Deploy -- profile '{settings.profile}'{' (dry run)' if dry_run or out_dir else ''}"
    )
    table.add_column("Stage", style="cyan")
    table.add_column("File")
    table.add_column("Statements", justify="right")
    table.add_column("Status")
    for entry in result.entries:
        table.add_row(
            entry.stage,
            entry.filename,
            str(entry.statement_count),
            "[green]ok[/green]" if entry.ok else f"[red]{entry.error}[/red]",
        )
    console.print(table)

    if result.failed:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #

_GATE_STYLE = {
    "PASS": "[green]PASS[/green]",
    "FAIL": "[red]FAIL[/red]",
    "REPORT": "[dim]REPORT[/dim]",
    "DEFERRED_TO_LOAD": "[yellow]DEFERRED[/yellow]",
}


@app.command()
def generate(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Output directory. Default: output.root/<profile>."),
    ] = None,
    data_root: Annotated[
        Path | None,
        typer.Option(
            "--data-root", help="Directory holding data/MANIFEST.yml. Default: repo root."
        ),
    ] = None,
) -> None:
    """Build the synthetic dataset and run the realism validation gates.

    Exits 1 if any gate with ``fail_build: true`` fails; the files are still
    written, marked ``"passed": false`` in ``manifest.json``, for inspection.
    """
    from risk_copilot.generation import GenerationError
    from risk_copilot.generation.pipeline import run_generation

    settings = _settings()
    try:
        result = run_generation(settings, data_root=data_root, out_dir=out_dir)
    except GenerationError as exc:
        console.print(f"[red]Generation failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Generation gates -- profile '{settings.profile}'")
    table.add_column("Gate", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("Blocks build", justify="center")
    table.add_column("Value")
    table.add_column("Detail", overflow="fold")
    for gate in result.gates:
        table.add_row(
            gate.name,
            _GATE_STYLE.get(gate.status.value, gate.status.value),
            "yes" if gate.fail_build else "no",
            gate.value,
            gate.detail,
        )
    console.print(table)
    console.print(f"Output: {result.out_dir}")
    if result.demo_entity_path:
        console.print(f"Demo entity (provisional): {result.demo_entity_path}")
    if result.passed:
        console.print("[green]All blocking gates passed.[/green]")
        return
    blocking = ", ".join(g.name for g in result.gates if g.blocks)
    console.print(f"[red]Blocking gate(s) failed:[/red] {blocking}")
    raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


@app.command()
def load(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Generated dataset. Default: output.root/<profile>."),
    ] = None,
    data_root: Annotated[
        Path | None,
        typer.Option(
            "--data-root", help="Directory holding data/MANIFEST.yml. Default: repo root."
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Delete and reload the tables this load writes, if they hold rows. "
            "Without it a non-empty target refuses the load.",
        ),
    ] = False,
    source: Annotated[
        bool,
        typer.Option(
            "--source/--no-source",
            help="Also load the AMLworld ledger and accounts into RAW_AMLWORLD_*.",
        ),
    ] = True,
    hold_back: Annotated[
        bool,
        typer.Option(
            "--hold-back/--no-hold-back",
            help="Leave legs at or after replay.hold_back_from for `riskcopilot replay`. "
            "--no-hold-back loads everything (the equivalence check's full load).",
        ),
    ] = True,
) -> None:
    """Upload a generated dataset and COPY it into RAW and GROUND_TRUTH.

    Refuses a dataset whose manifest failed a gate, or whose files changed since
    generation. Everything after the upload runs in one transaction and is
    rolled back if any post-load check fails.
    """
    from risk_copilot.pipeline.load import LoadError, open_bundle
    from risk_copilot.pipeline.load import load as run_load
    from risk_copilot.session import SessionError, open_session

    settings = _settings()
    try:
        cut = settings.replay.hold_back_from if hold_back else None
        bundle = open_bundle(settings, out_dir)
        with open_session(settings, unattended=True) as session:
            report = run_load(
                settings,
                session,
                bundle=bundle,
                data_root=data_root,
                replace=replace,
                source=source,
                hold_back=cut,
            )
    except (LoadError, SessionError, ConfigError) as exc:
        console.print(f"[red]Load failed; nothing was committed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    rows = Table(title=f"Loaded -- profile '{settings.profile}', run {report.run_key}")
    rows.add_column("Table", style="cyan")
    rows.add_column("Rows", justify="right")
    for table, count in report.rows.items():
        rows.add_row(table, f"{count:,}")
    console.print(rows)
    checks = Table(title="Post-load checks")
    checks.add_column("Check", style="cyan")
    checks.add_column("Status")
    checks.add_column("Value")
    checks.add_column("Detail", overflow="fold")
    for c in report.checks:
        checks.add_row(
            c.name, "[green]OK[/green]" if c.ok else "[red]FAIL[/red]", c.value, c.detail
        )
    console.print(checks)
    if report.held_back_from is not None:
        console.print(
            f"Held back for replay: legs at or after {report.held_back_from}. "
            "Stream them in with `riskcopilot replay`."
        )


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _ratio(numerator: Any, denominator: Any, value: Any) -> str:
    return f"{numerator}/{denominator} ({float(value):.3f})" if value is not None else "n/a"


@app.command("verify")
def verify_detection() -> None:
    """Refresh detection and check it against the generated truth (obligation 7).

    Exits 1 if a rule's signal count is outside validation.signal_yield (when that
    gate blocks) or the hero pin is not detected; 2 if the pin disagrees with the
    configuration or the loaded data. Its only writes are the refresh, the
    verified-pin file and one GROUND_TRUTH.EVAL_RUN row: it never routes signals.
    """
    from risk_copilot.pipeline.verify import CONFIRMED, VerifyError, read_pin
    from risk_copilot.pipeline.verify import verify as run_verify
    from risk_copilot.session import SessionError, open_session

    settings = _settings()
    try:
        pin = read_pin(settings)
        with open_session(settings, unattended=True) as session:
            report = run_verify(settings, session, pin)
    except VerifyError as exc:
        console.print(f"[red]Verify refused[/red]\n{exc}")
        raise typer.Exit(code=2) from exc
    except (SessionError, ConfigError) as exc:
        console.print(f"[red]Verify failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    lo, hi = report.bounds
    yields = Table(
        title=f"Signal yield -- profile '{settings.profile}', bounds [{lo}, {hi}]"
        + ("" if report.yield_blocks else " (reported, not blocking)")
    )
    for column in ("Rule", "Registered", "Signals", "Status"):
        yields.add_column(column, style="cyan" if column == "Rule" else None)
    for y in report.yields:
        yields.add_row(
            y.rule_id,
            "yes" if y.registered else "[red]no[/red]",
            f"{y.signals:,}",
            "[green]OK[/green]" if y.within_bounds else "[red]FAIL[/red]",
        )
    console.print(yields)

    detection = Table(title="Detection at party-month (reported, not gated)")
    for column in ("Rule", "Recall", "Precision", "FP legit / other / not in grain"):
        detection.add_column(column, style="cyan" if column == "Rule" else None)
    detection.add_column("Missed", justify="right")
    detection.add_column("FP control")
    detection.add_column("Unresolved evidence", justify="right")
    for m in report.metrics:
        control = (
            f"{m['controls_fired']} of {m['controls']} fired"
            if m["control_defined"]
            else "none defined"
        )
        detection.add_row(
            str(m["rule_id"]),
            _ratio(m["true_positive"], m["expected"], m["recall"]),
            _ratio(m["true_positive"], m["predicted"], m["precision"]),
            f"{m['fp_legit']} / {m['fp_other_illicit']} / {m['not_in_grain']}",
            str(m["missed"]),
            control,
            str(m["evidence_unresolved"]),
        )
    console.print(detection)

    hero = Table(title=f"Hero {report.party_id}: {report.status}")
    for column in ("Rule", "Detected", "Signal business key", "Case"):
        hero.add_column(column, style="cyan" if column == "Rule" else None)
    for h in report.hero:
        hero.add_row(
            h.rule_id,
            "[green]yes[/green]" if h.detected else "[red]no[/red]",
            ", ".join(h.signal_business_keys) or "-",
            ", ".join(h.case_ids) or "-",
        )
    console.print(hero)
    for rule_id in report.unrouted:
        console.print(
            f"[yellow]WARN[/yellow] {rule_id} fired for the hero but its signal is on no case "
            "yet; verify does not route (CALL the routing procedure, or let the task run)."
        )
    console.print(f"Hero result: {report.verified_path}")
    console.print(f"EVAL_RUN: {report.run_id}")
    if report.passed:
        console.print("[green]Verify passed.[/green]")
    else:
        reasons = ["signal yield"] if not report.yield_ok and report.yield_blocks else []
        reasons += [] if report.status == CONFIRMED else ["hero not detected"]
        console.print(f"[red]Verify failed:[/red] {', '.join(reasons)}")
    raise typer.Exit(code=report.exit_code)


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


@app.command()
def replay(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Generated dataset. Default: output.root/<profile>."),
    ] = None,
    inject: Annotated[
        str | None,
        typer.Option(
            "--inject",
            help="Release one host's pending burst now and exit (a replay.injections name).",
        ),
    ] = None,
    party: Annotated[
        str | None,
        typer.Option("--party", help="With --inject: the host. Default: the hero pin's party."),
    ] = None,
) -> None:
    """Stream the legs load held back into RAW, as if they were arriving live.

    One replay.sim_step of simulated time per replay.sleep_seconds, one
    transaction per tick; a restart skips the ticks already in. It never
    refreshes or routes: the target lag (or `verify`) refreshes, and the routing
    task routes. Do not run --inject while a streaming replay is running.
    """
    from risk_copilot.pipeline.load import LoadError, open_bundle
    from risk_copilot.pipeline.replay import ReplayError, TickResult, describe
    from risk_copilot.pipeline.replay import inject as run_inject
    from risk_copilot.pipeline.replay import replay as run_replay
    from risk_copilot.pipeline.verify import VerifyError, read_pin
    from risk_copilot.session import SessionError, open_session

    settings = _settings()
    if party and not inject:
        console.print("[red]--party needs --inject[/red]")
        raise typer.Exit(code=2)
    try:
        cut = settings.replay.hold_back_from
    except ConfigError as exc:
        console.print(f"[red]Configuration error[/red]\n{exc}")
        raise typer.Exit(code=2) from exc
    if cut is None and not inject:
        console.print(
            f"[red]Nothing to replay[/red]: replay.hold_back_from is null for profile "
            f"'{settings.profile}', so load holds nothing back."
        )
        raise typer.Exit(code=2)

    if inject:
        try:
            host = party or str(read_pin(settings)["party_id"])
            with open_session(settings, unattended=True) as session:
                done = run_inject(settings, session, inject, host)
        except VerifyError as exc:
            console.print(f"[red]Inject refused[/red]\n{exc}")
            raise typer.Exit(code=2) from exc
        except (ReplayError, SessionError, ConfigError) as exc:
            console.print(f"[red]Inject failed; nothing was committed[/red]\n{exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"Released {done.promoted} {done.cohort} legs for {done.party_id} "
            f"({done.first} .. {done.last}). Signals appear at the next refresh."
        )
        return

    def progress(result: TickResult, total: int) -> None:
        t = result.tick
        what = (
            "already in, skipped"
            if result.skipped
            else f"{result.source} source + {result.promoted} injected"
        )
        console.print(f"tick {t.index + 1}/{total} [{t.start} .. {t.end}): {what}")

    try:
        bundle = open_bundle(settings, out_dir)
        with open_session(settings, unattended=True) as session:
            report = run_replay(settings, session, bundle, on_tick=progress)
    except (ReplayError, LoadError, SessionError, ConfigError) as exc:
        console.print(f"[red]Replay stopped; the tick in progress was rolled back[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"Replayed {report.held_legs:,} legs from {report.cut}: {describe(report.ticks)}."
    )
    checks = Table(title="Post-replay checks")
    for column in ("Check", "Status", "Value", "Detail"):
        checks.add_column(column, style="cyan" if column == "Check" else None)
    for c in report.checks:
        checks.add_row(
            c.name, "[green]OK[/green]" if c.ok else "[red]FAIL[/red]", c.value, c.detail
        )
    console.print(checks)
    if not report.ok:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# app -- the Streamlit-in-Snowflake demo surface
# --------------------------------------------------------------------------- #

app_cli = typer.Typer(
    help="Render, run locally, or deploy the Streamlit app.", no_args_is_help=True
)
app.add_typer(app_cli, name="app")


@app_cli.command("render")
def app_render() -> None:
    """Write app/app_config.json from the resolved configuration."""
    from risk_copilot.pipeline.app import AppError, render_app_config

    settings = _settings()
    try:
        path = render_app_config(settings)
    except AppError as exc:
        console.print(f"[red]App render failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"Wrote {path}")


@app_cli.command("run")
def app_run(
    port: Annotated[int, typer.Option("--port", help="Local port.")] = 8501,
) -> None:
    """Render the config, then run the app locally against this profile's database."""
    import subprocess

    from risk_copilot.pipeline.app import MAIN_FILE, AppError, app_root, render_app_config

    settings = _settings()
    try:
        render_app_config(settings)
    except AppError as exc:
        console.print(f"[red]App render failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    # The app opens its own session from the same RC_* environment (.env was
    # loaded by the callback); the profile is passed explicitly.
    env = {**os.environ, "RC_PROFILE": settings.profile}
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_root(settings) / MAIN_FILE)]
    cmd += ["--server.port", str(port), "--server.headless", "true"]
    raise typer.Exit(code=subprocess.call(cmd, env=env))  # noqa: S603 -- fixed argv


@app_cli.command("deploy")
def app_deploy() -> None:
    """Upload app/ and (re)create the STREAMLIT object on the container runtime."""
    from risk_copilot.pipeline.app import AppError, deploy_app
    from risk_copilot.session import SessionError, open_session

    settings = _settings()
    try:
        with open_session(settings, unattended=True) as session:
            result = deploy_app(settings, session, echo=console.print)
    except (AppError, SessionError) as exc:
        console.print(f"[red]App deploy failed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]Deployed[/green] {result.fq_name}: {len(result.files)} files, "
        f"pool {result.compute_pool}, warehouse {result.warehouse}"
    )
    console.print(f"Open: {result.url or '(Snowsight > Projects > Streamlit)'}")


@app_cli.command("reset-cases")
def app_reset_cases(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Reopen every case and delete its evidence, drafts, reviews and sign-offs.

    For re-running the demo on the same cases. Calls the RESET_CASES procedure,
    which the app's Governance page also calls. Signals, case-signal links and
    the audit log are kept; the reset logs itself there.
    """
    from risk_copilot.pipeline.app import AppError, reset_cases
    from risk_copilot.session import SessionError, open_session

    settings = _settings()
    try:
        with open_session(settings, unattended=True) as session:
            preview = reset_cases(settings, session, dry_run=True)
            if not preview.total_rows and not preview.cases_reopened:
                console.print("Nothing to reset: every case is OPEN with no workflow rows.")
                return
            table = Table(title=f"Reset in {settings.db()} (profile '{settings.profile}')")
            table.add_column("Table", style="cyan")
            table.add_column("Rows", justify="right")
            for name, n in preview.rows.items():
                table.add_row(f"{name} (deleted)", f"{n:,}")
            table.add_row("CASE_RECORD (reopened)", f"{preview.cases_reopened:,}")
            console.print(table)
            if not yes and not typer.confirm("Delete these rows and reopen the cases?"):
                console.print("Cancelled; nothing changed.")
                raise typer.Exit(code=1)
            result = reset_cases(settings, session, dry_run=False)
    except (AppError, SessionError) as exc:
        console.print(f"[red]Reset failed; nothing changed[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    n = result.cases_reopened
    console.print(
        f"[green]Reset[/green]: {result.total_rows:,} workflow rows deleted, "
        f"{n} case{'' if n == 1 else 's'} reopened. The audit log is kept."
    )


# --------------------------------------------------------------------------- #
# Stubs -- implemented in later phases, declared now so the surface is stable.
# --------------------------------------------------------------------------- #


@app.command("eval")
def run_eval() -> None:
    """Run the evaluation suites (detection F1, text-to-SQL, adversarial)."""
    console.print("[yellow]Not yet implemented[/yellow] -- Phase 5 (evaluation).")
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
