"""Idempotent, ordered SQL deployer.

The build is ~30 SQL files with hard ordering dependencies. Applying them by
hand is how a demo environment drifts from the repository. This module renders
every file as a Jinja template against :meth:`Settings.sql_context` and applies
them in a fixed order.

Two invariants:

* **No literal object names in SQL.** Every database, schema, warehouse, role
  and stage name arrives from configuration, which is what makes ``dev`` and
  ``demo`` coexist in one account. :meth:`Deployer.lint` enforces this.
* **Every stage is re-runnable.** Statements use ``CREATE OR ALTER`` or
  ``IF NOT EXISTS``, so re-applying a stage is the normal way to ship a change
  and never a destructive act.

Snowflake DCM (``snow dcm``) is the heavier alternative and is the right answer
for a production deployment; a templated runner is kept here because it needs no
additional account objects and keeps the rendered SQL inspectable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from risk_copilot.config import ConfigError, Settings
from risk_copilot.session import SessionError, open_session

logger = logging.getLogger(__name__)

#: Fixed stage order. Each stage is a directory under ``sql/``; the numeric
#: prefix keeps the filesystem listing and the execution order identical.
STAGE_ORDER: tuple[tuple[str, str], ...] = (
    ("bootstrap", "00_bootstrap"),
    ("ddl", "01_ddl"),
    ("dynamic_tables", "02_dynamic_tables"),
    ("streams_tasks", "03_streams_tasks"),
    ("governance", "04_governance"),
    ("policy", "05_policy"),
    ("search", "06_search"),
    ("semantic", "07_semantic"),
    ("agent", "08_agent"),
    ("ml", "09_ml"),
    ("automations", "10_automations"),
    ("procedures", "procedures"),
    # Grants on procedures and the semantic view, so after both.
    ("app_access", "11_app_access"),
)

#: Queries run by ``riskcopilot verify``, rendered like stage files but never
#: deployed: they read and check, and are linted with the stages.
VERIFY_DIR = "verify"

#: Fragments included into a statement rather than rendered on their own, so
#: :meth:`Deployer.render` never sees their source: one SELECT each, no ``;``.
INCLUDED_DIRS = ("rules", f"{VERIFY_DIR}/controls")

#: Object names that must never appear literally in a template. Catches the
#: regression where someone pastes working ad-hoc SQL into a file and the
#: profile suffix stops being applied.
_FORBIDDEN_LITERALS = re.compile(
    r"\b(RISK_COPILOT|RC_DEV_WH|RC_PIPELINE_WH|RC_DEMO_WH|RC_ADMIN|RC_SERVICE_ROLE|RC_PUBLIC_APP\w*|RC_PUBLIC_OFFICER\w*"
    r"|COMPLIANCE_OFFICER|BRANCH_ANALYST_\w+)\b"
)

#: Adjacent string literals on consecutive lines. Snowflake does NOT concatenate
#: them the way C does, so a COMMENT split across lines this way is a syntax
#: error rather than one string -- and the error surfaces only at execution,
#: after earlier statements have already applied.
_STRING_CONTINUATION = re.compile(r"'[,;]?[ \t]*\r?\n[ \t]*'")


@dataclass(slots=True)
class FileResult:
    stage: str
    filename: str
    statement_count: int
    ok: bool = True
    error: str = ""


@dataclass(slots=True)
class DeployResult:
    entries: list[FileResult] = field(default_factory=list)

    @property
    def failed(self) -> list[FileResult]:
        return [e for e in self.entries if not e.ok]


def split_statements(sql: str) -> list[str]:
    """Split a script into executable statements.

    Only a semicolon at the top level ends a statement. Scanning is required
    rather than ``sql.split(";")``, because a semicolon is perfectly legal
    inside a string literal -- ``COMMENT = 'Append-only; never updated'`` is the
    common case here -- as well as inside a comment or a dollar-quoted procedure
    body. Splitting on every semicolon tore those statements in half and the
    resulting fragments failed at execution, after earlier statements in the
    file had already applied.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]
        pair = sql[index : index + 2]

        # Dollar-quoted body: copy verbatim to its terminator.
        if pair == "$$":
            end = sql.find("$$", index + 2)
            end = length if end == -1 else end + 2
            buffer.append(sql[index:end])
            index = end
            continue

        # Line comment: copy to end of line.
        if pair == "--":
            end = sql.find("\n", index)
            end = length if end == -1 else end
            buffer.append(sql[index:end])
            index = end
            continue

        # Block comment: copy to its terminator.
        if pair == "/*":
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            buffer.append(sql[index:end])
            index = end
            continue

        # String literal. A doubled quote is an escaped quote, not a terminator.
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if sql[cursor] == "'":
                    if sql[cursor : cursor + 2] == "''":
                        cursor += 2
                        continue
                    cursor += 1
                    break
                cursor += 1
            buffer.append(sql[index:cursor])
            index = cursor
            continue

        # Quoted identifier.
        if char == '"':
            end = sql.find('"', index + 1)
            end = length if end == -1 else end + 1
            buffer.append(sql[index:end])
            index = end
            continue

        if char == ";":
            statements.append("".join(buffer))
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    statements.append("".join(buffer))

    kept: list[str] = []
    for chunk in statements:
        # Drop chunks that are only whitespace or comments -- a trailing comment
        # after the last semicolon would otherwise be sent as a statement.
        stripped = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if stripped:
            kept.append(chunk.strip())
    return kept


class Deployer:
    """Renders and applies the SQL stages for one profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sql_root = settings.repo_root / "sql"
        self.env = Environment(
            # Lets a stage include a shared fragment -- a rule's detection SELECT
            # lives once in sql/rules/ and is hashed from there (RULE_DEFINITION).
            loader=FileSystemLoader(str(self.sql_root)),
            # StrictUndefined: a typo in a template variable must fail loudly at
            # render time rather than silently emitting an empty identifier and
            # producing SQL that creates the wrong object.
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,  # noqa: S701 - rendering SQL, not HTML
        )

    # ------------------------------------------------------------------ #
    # Discovery and rendering
    # ------------------------------------------------------------------ #

    def stage_files(self, stage_key: str) -> list[Path]:
        """Return the ``.sql`` files for a stage, in lexical order."""
        directory = dict(STAGE_ORDER).get(stage_key)
        if directory is None:
            raise ConfigError(
                f"Unknown stage {stage_key!r}; expected one of {[s for s, _ in STAGE_ORDER]}"
            )
        path = self.sql_root / directory
        if not path.is_dir():
            return []
        return sorted(p for p in path.glob("*.sql") if not p.name.startswith("_"))

    def verify_files(self) -> list[Path]:
        """The ``verify`` queries, in lexical (execution) order."""
        path = self.sql_root / VERIFY_DIR
        return sorted(p for p in path.glob("*.sql") if not p.name.startswith("_"))

    def _display(self, path: Path) -> str:
        """Return a repo-relative path for messages, falling back to the absolute one.

        ``Path.relative_to`` raises for anything outside the repository, and an
        error message is the worst place to add a second exception.
        """
        try:
            return str(path.relative_to(self.settings.repo_root))
        except ValueError:
            return str(path)

    def render(self, path: Path) -> str:
        """Render one SQL template.

        Raises:
            ConfigError: If the template references an undefined variable or
                contains a forbidden literal object name.
        """
        source = path.read_text(encoding="utf-8")
        try:
            rendered = self.env.from_string(source).render(**self.settings.sql_context())
        except TemplateError as exc:
            raise ConfigError(
                f"{self._display(path)}: template error: {exc}\n"
                "Every identifier must come from the sql_context contract "
                "(see sql/README.md)."
            ) from exc

        # Lint the SOURCE, not the rendered output: the rendered output legitimately
        # contains these names.
        if match := _FORBIDDEN_LITERALS.search(source):
            raise ConfigError(
                f"{self._display(path)}: contains the literal object "
                f"name {match.group(0)!r}. Use the templated form instead (for example "
                f"{{{{ db }}}}, {{{{ wh.pipeline }}}}, {{{{ role.admin }}}}) so the profile "
                f"suffix is applied and dev cannot overwrite demo."
            )

        # Checked on the rendered text, since a Jinja block could introduce it.
        if match := _STRING_CONTINUATION.search(rendered):
            line_number = rendered[: match.start()].count("\n") + 1
            raise ConfigError(
                f"{self._display(path)}:{line_number}: two string "
                "literals on consecutive lines. Snowflake does not concatenate adjacent "
                "literals, so this is a syntax error, not one string. Put the literal on a "
                "single line, or join the parts with ||."
            )
        return rendered

    def lint(self) -> list[str]:
        """Render every stage without executing, returning error messages.

        Cheap enough for a pre-commit hook and CI, and catches the whole class of
        "the template was never rendered" mistakes offline.
        """
        errors: list[str] = []
        for path in [p for key, _ in STAGE_ORDER for p in self.stage_files(key)] + (
            self.verify_files()
        ):
            try:
                self.render(path)
            except ConfigError as exc:
                errors.append(str(exc))
        # Included fragments (sql/rules/, the verify controls, macro files) are
        # rendered inside another file, so render() never sees their source;
        # lint it here.
        for directory in INCLUDED_DIRS:
            for path in sorted((self.sql_root / directory).glob("*.sql")):
                source = path.read_text(encoding="utf-8")
                if match := _FORBIDDEN_LITERALS.search(source):
                    errors.append(
                        f"{self._display(path)}: contains the literal object name "
                        f"{match.group(0)!r}"
                    )
                if ";" in source:
                    errors.append(
                        f"{self._display(path)}: an included fragment is one SELECT inside "
                        "another statement; a ';' would end that statement early"
                    )
        for path in sorted((self.sql_root / VERIFY_DIR).glob("_*.sql")):
            if match := _FORBIDDEN_LITERALS.search(path.read_text(encoding="utf-8")):
                errors.append(
                    f"{self._display(path)}: contains the literal object name {match.group(0)!r}"
                )
        return errors

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def run(
        self,
        stages: list[str] | None = None,
        *,
        dry_run: bool = False,
        out_dir: Path | None = None,
    ) -> DeployResult:
        """Apply the requested stages in order.

        Args:
            stages: Stage keys. ``None`` applies all of them, in
                :data:`STAGE_ORDER`.
            dry_run: Render only; never connect to Snowflake.
            out_dir: Write rendered SQL here. Implies ``dry_run``.

        Execution stops at the first failing file: later stages depend on earlier
        ones, so continuing would only produce cascading errors that obscure the
        real cause.
        """
        selected = self._select_stages(stages)
        result = DeployResult()

        if dry_run or out_dir is not None:
            for stage_key, path in selected:
                rendered = self.render(path)
                statements = split_statements(rendered)
                if out_dir is not None:
                    target = out_dir / dict(STAGE_ORDER)[stage_key] / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(rendered, encoding="utf-8")
                result.entries.append(FileResult(stage_key, path.name, len(statements)))
            return result

        with open_session(self.settings) as session:
            self._use_context(session)
            for stage_key, path in selected:
                entry = self._apply_file(session, stage_key, path)
                result.entries.append(entry)
                if not entry.ok:
                    logger.error(
                        "Stopping: %s/%s failed and later stages depend on it.",
                        stage_key,
                        path.name,
                    )
                    break
        return result

    def _select_stages(self, stages: list[str] | None) -> list[tuple[str, Path]]:
        keys = [key for key, _ in STAGE_ORDER]
        if stages:
            requested = [s.strip().lower() for s in stages]
            if unknown := [s for s in requested if s not in keys]:
                raise ConfigError(f"Unknown stage(s) {unknown}; expected from {keys}")
            # Always apply in canonical order regardless of flag order, so a
            # mis-ordered command line cannot produce a broken deploy.
            keys = [k for k in keys if k in requested]

        selected: list[tuple[str, Path]] = []
        for key in keys:
            files = self.stage_files(key)
            if not files:
                logger.debug("Stage %s has no SQL files yet; skipping.", key)
            selected.extend((key, path) for path in files)

        if not selected:
            raise ConfigError(
                "No SQL files found for the selected stages. Expected templates under "
                f"{self.sql_root}"
            )
        return selected

    def _use_context(self, session: object) -> None:
        """Set role and warehouse explicitly, rather than trusting session defaults."""
        from risk_copilot.session import execute

        role = self.settings.obj(self.settings.roles.admin)
        warehouse = self.settings.obj(self.settings.warehouse("dev").name)
        for statement in (f"USE ROLE {role}", f"USE WAREHOUSE {warehouse}"):
            try:
                execute(session, statement)
            except SessionError as exc:
                # Bootstrap legitimately runs before the role and warehouse
                # exist, so this is informational.
                logger.warning("%s failed (expected before bootstrap): %s", statement, exc)

    def _apply_file(self, session: object, stage_key: str, path: Path) -> FileResult:
        from risk_copilot.session import execute

        rendered = self.render(path)
        statements = split_statements(rendered)
        logger.info("Applying %s/%s (%d statements)", stage_key, path.name, len(statements))

        for index, statement in enumerate(statements, start=1):
            try:
                execute(session, statement)
            except SessionError as exc:
                return FileResult(
                    stage_key,
                    path.name,
                    len(statements),
                    ok=False,
                    error=f"statement {index}/{len(statements)}: {exc}",
                )
        return FileResult(stage_key, path.name, len(statements))
