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
* ``replay``    drive the replay harness
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
# Stubs -- implemented in later phases, declared now so the surface is stable.
# --------------------------------------------------------------------------- #


@app.command()
def load() -> None:
    """Upload generated data to a stage and COPY INTO the landing tables."""
    console.print("[yellow]Not yet implemented[/yellow] -- Phase 3 (load).")
    raise typer.Exit(code=2)


@app.command()
def replay() -> None:
    """Stream pre-generated transactions into the live pipeline."""
    console.print("[yellow]Not yet implemented[/yellow] -- Phase 3 (replay harness).")
    raise typer.Exit(code=2)


@app.command("eval")
def run_eval() -> None:
    """Run the evaluation suites (detection F1, text-to-SQL, adversarial)."""
    console.print("[yellow]Not yet implemented[/yellow] -- Phase 5 (evaluation).")
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
