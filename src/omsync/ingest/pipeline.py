"""The import loop: read statement files, plan each one, and apply the plans as batches.

`omsync import` and `omsync sync` both call import_files, and the web app can too. It prints
nothing. Callers show the ImportReport. Each file yields one FileResult, and the results come in
phases, as the loop always has: first the files that cannot be read, then the files that cannot
be staged, then one result per remaining file in input order.

Without apply nothing is written: not the database, and not copies of files from outside the
drop folder. With apply, each file is imported as its own batch. A file from outside the drop
folder is copied into it first, and a file already imported is never copied. When any file was
imported, one more batch then links the proposals that need no review (see
omsync.reconcile.auto). That runs here, not in each caller, so the terminal import, the web import,
and every sync share it. A sync of several providers links once per provider, since each calls this.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError, preview_digest
from omsync.core.settings import Settings
from omsync.ingest.dropfolder import (
    PendingFile,
    destination_for,
    location_of,
    pending_files,
    stage_external,
    stored_path,
)
from omsync.ingest.formats.base import FormatError, Statement, StatementFormat
from omsync.ingest.formats.registry import format_for
from omsync.ingest.importer import ImportPlan, apply_import, plan_import
from omsync.ledger.accounts import AccountError, UnmappedAccount
from omsync.ledger.models import Account, StatementFile
from omsync.reconcile.auto import AutoReconcile, reconcile_after_import


@dataclass(frozen=True)
class FileResult:
    """What happened to one file. It is an error, already imported, or a planned or applied
    import with its counts. name is the file name only. account_last4 is shown as ••last4, or as
    ???? when the account has none, with account_alias_last4 beside it (see account_digits). error
    names the file and is safe to show. For an unmapped account, error gives the last four digits
    only, unmapped_institution and unmapped_last4 are set, and unmapped_type_hint is the account
    type the file reported, if any. digest identifies the changes this file's import would make, so
    a preview can be checked before it is applied.
    """

    name: str
    account_last4: str | None = None
    account_alias_last4: str | None = None
    joined: int = 0
    created: int = 0
    review: int = 0
    already_imported: bool = False
    error: str | None = None
    unmapped_institution: str | None = None
    unmapped_type_hint: str | None = None
    unmapped_last4: str | None = None
    digest: str = ""


@dataclass(frozen=True)
class ImportReport:
    """The outcome of import_files. applied is False for a preview, which wrote nothing. auto is
    the links recorded after the import; it is empty for a preview or when no file was imported."""

    applied: bool
    files: tuple[FileResult, ...] = ()
    auto: AutoReconcile = field(default_factory=AutoReconcile)

    @property
    def failures(self) -> int:
        return sum(1 for result in self.files if result.error is not None)

    @property
    def planned(self) -> int:
        """Files that would import, counted in a preview. Zero after an apply."""
        if self.applied:
            return 0
        return sum(
            1 for result in self.files if result.error is None and not result.already_imported
        )

    @property
    def identity(self) -> str:
        """A token for what a preview would import: every file's name, status, and planned changes.
        The import carries the token of the preview the person saw, and refuses to run if any file
        or the ledger has changed since, even when the counts still match."""
        body = [
            [result.name, result.error or "", result.already_imported, result.digest]
            for result in self.files
        ]
        return hashlib.sha256(json.dumps(body).encode()).hexdigest()

    @property
    def review(self) -> int:
        return sum(result.review for result in self.files)


def import_files(
    session: Session,
    settings: Settings,
    files: list[PendingFile],
    *,
    apply: bool,
) -> ImportReport:
    """Plan each file and, with apply, import it as its own batch. See the module docstring.

    A file whose institution is None is refused. A file that is not under statements/ is copied
    into the drop folder under its institution, and its statement account ID, if apply is set.
    """
    run = _Run(session, settings, apply=apply)
    parsed = run.parse(files)
    staged = run.stage(parsed)
    for item in staged:
        run.import_one(item)
    return run.report()


def import_institution(
    session: Session, settings: Settings, institution_key: str, *, apply: bool
) -> ImportReport:
    """import_files for every pending file in one institution's folder. A file already imported is
    not pending, so a repeated apply does not import it twice."""
    return import_files(
        session, settings, pending_files(session, settings, institution_key), apply=apply
    )


@dataclass
class _Parsed:
    pending: PendingFile
    external: bool  # outside statements/, so apply copies it into the drop folder
    fmt: StatementFormat
    statement: Statement
    stored: str = ""  # the path the statement file is recorded under

    @property
    def institution(self) -> str:
        assert self.pending.institution_key is not None  # parse refuses a file without one
        return self.pending.institution_key


class _Run:
    """One import run: what it has done so far, and the results it has produced."""

    def __init__(self, session: Session, settings: Settings, *, apply: bool) -> None:
        self.session = session
        self.settings = settings
        self.apply = apply
        self.results: list[FileResult] = []
        self.imported = False  # any file was imported

    def parse(self, files: list[PendingFile]) -> list[_Parsed]:
        """Read each file with its format. A file that cannot be read gets an error result."""
        parsed: list[_Parsed] = []
        for pending in files:
            name = pending.path.name
            if pending.institution_key is None:
                self.results.append(
                    FileResult(
                        name,
                        error=(
                            f"{name}: no institution. Put it in statements/<INSTITUTION>/, "
                            "or pass --institution KEY."
                        ),
                    )
                )
                continue
            try:
                fmt = format_for(pending.institution_key, pending.path)
                statement = fmt.read(
                    pending.path, statement_account_id=pending.statement_account_id
                )
            except FormatError as exc:
                self.results.append(FileResult(name, error=str(exc)))
                continue
            external = location_of(self.settings, pending.path) is None
            parsed.append(_Parsed(pending, external, fmt, statement))
        return parsed

    def stage(self, parsed: list[_Parsed]) -> list[_Parsed]:
        """Set where each file is recorded, and drop the files that could not be staged.

        With apply, outside files are copied into the drop folder first. Without it, only the
        destination is worked out. A file already imported is not copied, and a file repeated in
        this run is copied once.
        """
        imported = set(self.session.scalars(select(StatementFile.sha256)).all())
        staged: dict[str, str] = {}
        return [item for item in parsed if self._locate(item, imported, staged)]

    def _locate(self, item: _Parsed, imported: set[str], staged: dict[str, str]) -> bool:
        """Set the item's stored path. Returns False, after an error result, if staging failed."""
        pending = item.pending
        sha256 = item.statement.sha256
        if not item.external:
            item.stored = stored_path(self.settings, pending.path)
            return True
        if sha256 in imported:
            return True  # the plan will say already imported, so nothing is copied
        if sha256 in staged:
            item.stored = staged[sha256]
            return True
        stage = stage_external if self.apply else _preview_destination
        account_id = (
            pending.statement_account_id or item.statement.statement_account_id or "default"
        )
        statement_year = (
            item.statement.period_end.year
            if item.statement.period_end
            else (
                item.statement.period_start.year
                if item.statement.period_start
                else date.today().year
            )
        )
        try:
            target = stage(
                pending.path,
                self.settings,
                item.institution,
                account_id,
                year=statement_year,
            )
        except (OSError, ValueError) as exc:
            name = pending.path.name
            self.results.append(FileResult(name, error=f"{name}: could not stage the file: {exc}"))
            return False
        item.stored = staged[sha256] = stored_path(self.settings, target)
        return True

    def import_one(self, item: _Parsed) -> None:
        """Plan one file, and with apply, apply the plan as its own batch."""
        name = item.pending.path.name
        try:
            plan = plan_import(
                self.session,
                item.statement,
                item.fmt,
                institution_key=item.institution,
                stored_path=item.stored,
                original_name=name,
            )
        except UnmappedAccount as exc:
            self.results.append(
                FileResult(
                    name,
                    error=(
                        f"{name}: no account at {item.institution} is mapped to the "
                        f"statement account ••{exc.statement_account_last4}"
                    ),
                    unmapped_institution=item.institution,
                    unmapped_type_hint=item.statement.account_type_hint,
                    unmapped_last4=exc.statement_account_last4,
                )
            )
            return
        except AccountError as exc:
            self.results.append(FileResult(name, error=f"{name}: {exc}"))
            return
        if plan.already_imported:
            self.results.append(FileResult(name, already_imported=True))
            return
        if self.apply and not self._apply(item, plan):
            return
        account = self.session.get(Account, plan.account_id)
        self.results.append(
            FileResult(
                name,
                account_last4=_last4(account),
                account_alias_last4=None if account is None else account.alias_last4,
                joined=plan.joined,
                created=plan.created,
                review=plan.review,
                digest=preview_digest(plan.preview),
            )
        )

    def _apply(self, item: _Parsed, plan: ImportPlan) -> bool:
        """Apply a plan under a key of its own, so a repeat of this attempt cannot double-apply."""
        name = item.pending.path.name
        try:
            apply_import(
                self.session,
                plan,
                idempotency_key=f"import:{item.statement.sha256}:{uuid4()}",
            )
        except BatchError as exc:
            self.results.append(FileResult(name, error=f"{name}: {exc}"))
            return False
        self.imported = True
        return True

    def report(self) -> ImportReport:
        """The file results, and the automatic links made after the import."""
        auto = (
            reconcile_after_import(self.session)
            if self.apply and self.imported
            else AutoReconcile()
        )
        return ImportReport(applied=self.apply, files=tuple(self.results), auto=auto)


def _preview_destination(
    path: Path,
    settings: Settings,
    institution_key: str,
    statement_account_id: str | None,
    year: int | str | None = None,
) -> Path:
    """The preview's stand-in for stage_external: the same destination, without the copy."""
    return destination_for(settings, path, institution_key, statement_account_id, year=year)


def _last4(account: Account | None) -> str | None:
    return account.last4 if account is not None and account.last4 else None
