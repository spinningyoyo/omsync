"""Syncs started from the Data sync page. Each job runs in a background thread of the server
process, one job at a time, and shares the sync lock with the CLI. When a connector asks a
question, such as for an MFA code, the question waits on the page for an answer, or for a timeout.
A connector can also show a notice, such as a QR to scan, and a job can be cancelled: the bank in
progress stops at its next step, and no later bank starts.

Job state lives in memory and is lost when the server restarts; the sync log keeps the outcome of
every sync. An answer is handed to the connector and is never stored, logged, or shown again.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import ConnectorError, SyncCancelled
from omsync.ingest.discovery import mask_account_numbers
from omsync.ingest.pipeline import ImportReport
from omsync.ingest.runs import SyncBusy
from omsync.ingest.sync import SyncResult
from omsync.ledger.models import ProviderKind, SyncStatus
from omsync.sync.run import ALL, ProviderSync, sync_all, sync_one

logger = logging.getLogger(__name__)

PROMPT_TIMEOUT_SECONDS = 600.0
CONFIRMED = "confirmed"  # the answer a button-only question's form sends
ANSWER_LIMIT = 200
BUSY_MESSAGE = "Another sync is running in a terminal. Try again when it finishes."
ALREADY_RUNNING = "A sync is already running. Wait for it to finish."
VAULT_IN_USE = "A sync is running. Change the vault when it finishes."
NO_QUESTION = "No question is waiting for an answer."
NOT_RUNNING = "No sync is running."
EMPTY_ANSWER = "Type the answer first."
LONG_ANSWER = f"The answer is longer than {ANSWER_LIMIT} characters."


class SyncRefused(Exception):
    """A request the runner will not start or answer. The message is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JobPhase(StrEnum):
    idle = "idle"
    running = "running"
    finished = "finished"


class ProviderPhase(StrEnum):
    queued = "queued"
    running = "running"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    needs_action = "needs_action"
    cancelled = "cancelled"


@dataclass(frozen=True)
class Question:
    number: int
    provider_key: str
    text: str
    secret: bool
    digits: int | None  # the digits of a one-time code, so the page can send it once complete
    button: str | None = None  # a question answered by this button alone, with no text


@dataclass(frozen=True)
class Notice:
    """A message a connector shows that needs no answer, such as what to do on your phone. image
    is a data:image URL, such as a QR to scan. number is shared with questions, so a poll can tell
    whether what the page shows has changed."""

    number: int
    provider_key: str
    text: str
    image: str | None


@dataclass(frozen=True)
class ProviderProgress:
    key: str
    phase: ProviderPhase
    message: str | None = None
    items_fetched: int = 0
    created: int = 0
    joined: int = 0
    review: int = 0
    auto_linked: int = 0  # links recorded automatically after the import
    linked: tuple[str, ...] = ()  # last four digits of statement accounts the sync linked
    unlinked: tuple[str, ...] = ()  # last four digits of those no account is linked to yet


@dataclass(frozen=True)
class JobView:
    """A copy of the current job's state, safe to read outside the runner's lock. With no job yet
    it is idle with no providers. scope is ALL or one provider's key."""

    scope: str
    phase: JobPhase
    providers: tuple[ProviderProgress, ...]
    question: Question | None
    notice: Notice | None = None
    cancelling: bool = False  # cancel was asked and the job has not ended yet


@dataclass
class _Pending:
    question: Question
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


@dataclass
class _Job:
    scope: str
    providers: dict[str, ProviderProgress]
    bank_keys: list[str] = field(default_factory=list)
    receipt_keys: list[str] = field(default_factory=list)
    phase: JobPhase = JobPhase.running
    current: str | None = None
    pending: _Pending | None = None
    notice: Notice | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


class WebPrompt:
    """The Prompt of a web sync. ask() puts the question on the page and blocks the worker until
    it is answered, the job is cancelled, or the timeout passes. A timeout returns "", which a
    connector reports as no code entered, so the provider ends as needing you. show() puts a notice
    in the question's place."""

    def __init__(self, runner: "SyncRunner", job: _Job, timeout: float) -> None:
        self._runner = runner
        self._job = job
        self._timeout = timeout

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        return self._wait(Question(0, "", question, secret, digits))

    def confirm(self, question: str, *, button: str) -> bool:
        return self._wait(Question(0, "", question, False, None, button)) == CONFIRMED

    def _wait(self, asked: Question) -> str:
        pending = self._runner._open_question(self._job, asked)
        pending.event.wait(self._timeout)
        answer = self._runner._close_question(self._job, pending)
        if self.cancelled():
            raise SyncCancelled
        return answer

    def show(self, message: str, *, image: str | None = None) -> None:
        self._runner._show(self._job, message, image)

    def cancelled(self) -> bool:
        return self._job.cancel.is_set()


class SyncRunner:
    """Runs syncs in the background and keeps the state of the current or last one. Only one job
    runs at a time; starting another while one runs is refused."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        vault: Vault | None,
        settings: Settings,
        *,
        prompt_timeout: float = PROMPT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._vault = vault
        self._settings = settings
        self._prompt_timeout = prompt_timeout
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._questions = 0  # questions and notices, so each has its own number

    def use_vault(self, vault: Vault | None, settings: Settings) -> None:
        """Run later syncs with this vault and settings. Raises SyncRefused while a job runs."""
        with self._lock:
            if self._job is not None and self._job.phase is JobPhase.running:
                raise SyncRefused(VAULT_IN_USE)
            self._vault = vault
            self._settings = settings

    def start(
        self,
        scope: str,
        bank_keys: list[str],
        receipt_keys: list[str] | None = None,
    ) -> None:
        """Start a job that syncs bank providers and receipt providers."""
        r_keys = list(receipt_keys or [])
        with self._lock:
            if self._job is not None and self._job.phase is JobPhase.running:
                raise SyncRefused(ALREADY_RUNNING)
            providers: dict[str, ProviderProgress] = {
                key: ProviderProgress(key, ProviderPhase.queued) for key in [*bank_keys, *r_keys]
            }
            job = _Job(
                scope=scope, providers=providers, bank_keys=list(bank_keys), receipt_keys=r_keys
            )
            self._job = job
        threading.Thread(target=self._work, args=(job,), name="omsync-sync", daemon=True).start()

    def answer(self, text: str) -> None:
        """Give the answer to the question the worker is waiting on. Raises SyncRefused when no
        question is waiting, or the answer is empty or too long."""
        value = text.strip()
        with self._lock:
            pending = self._waiting_question()
            if pending is None:
                raise SyncRefused(NO_QUESTION)
            if not value:
                raise SyncRefused(EMPTY_ANSWER)
            if len(value) > ANSWER_LIMIT:
                raise SyncRefused(LONG_ANSWER)
            pending.answer = value
            pending.event.set()

    def cancel(self) -> None:
        """Cancel the running job. A question waiting for an answer stops waiting, the bank in
        progress stops at its next step, and no later bank starts. Raises SyncRefused when no job
        runs."""
        with self._lock:
            job = self._job
            if job is None or job.phase is not JobPhase.running:
                raise SyncRefused(NOT_RUNNING)
            job.cancel.set()
            job.notice = None
            if job.pending is not None:
                job.pending.event.set()

    def view(self) -> JobView:
        with self._lock:
            job = self._job
            if job is None:
                return JobView(scope="", phase=JobPhase.idle, providers=(), question=None)
            pending = self._waiting_question()
            running = job.phase is JobPhase.running
            return JobView(
                scope=job.scope,
                phase=job.phase,
                providers=tuple(job.providers.values()),
                question=pending.question if pending is not None and running else None,
                notice=job.notice,
                cancelling=running and job.cancel.is_set(),
            )

    def _waiting_question(self) -> _Pending | None:
        """The unanswered question of the running job, if any. Call with the lock held."""
        job = self._job
        if job is None or job.pending is None or job.pending.answer is not None:
            return None
        if job.cancel.is_set():
            return None
        return job.pending

    def _work(self, job: _Job) -> None:
        session: Session | None = None
        try:
            session = self._session_factory()
            prompt = WebPrompt(self, job, self._prompt_timeout)
            if job.scope == ALL:
                sync_all(
                    session,
                    self._settings,
                    vault=self._vault,
                    prompt=prompt,
                    on_progress=lambda key, outcome: self._progress(job, key, outcome),
                )
            else:
                for key in [*job.bank_keys, *job.receipt_keys]:
                    self._sync_single(job, session, prompt, key)
            if job.cancel.is_set():
                self._cancel_unstarted(job)
        except SyncBusy:
            self._fail_unfinished(job, BUSY_MESSAGE)
        except ConnectorError as exc:
            self._fail_unfinished(job, str(exc))
        except Exception as exc:  # the worker must never take the server down
            logger.exception("The sync worker failed unexpectedly")
            self._fail_unfinished(job, f"unexpected error ({type(exc).__name__})")
        finally:
            if session is not None:
                session.close()
            with self._lock:
                job.phase = JobPhase.finished
                job.pending = None
                job.notice = None
                job.current = None

    def _sync_single(self, job: _Job, session: Session, prompt: WebPrompt, key: str) -> None:
        self._progress(job, key, None)
        try:
            result = sync_one(
                session,
                self._settings,
                provider_key=key,
                vault=self._vault,
                prompt=prompt,
            )
        except ConnectorError as exc:
            self._progress(job, key, ProviderSync(key, None, str(exc)))
            return
        self._progress(job, key, ProviderSync(key, result, None))

    def _progress(self, job: _Job, key: str, outcome: ProviderSync | None) -> None:
        with self._lock:
            if outcome is None:
                job.current = key
                _update(job, key, phase=ProviderPhase.running)
                return
            job.current = None
            _update(job, key, **_outcome_fields(outcome))

    def _fail_unfinished(self, job: _Job, message: str) -> None:
        with self._lock:
            for key, progress in job.providers.items():
                if progress.phase not in (
                    ProviderPhase.succeeded,
                    ProviderPhase.failed,
                    ProviderPhase.needs_action,
                ):
                    _update(job, key, phase=ProviderPhase.failed, message=message)

    def _cancel_unstarted(self, job: _Job) -> None:
        """Mark the providers a cancelled job never reached."""
        with self._lock:
            for key, progress in job.providers.items():
                if progress.phase is ProviderPhase.queued:
                    _update(job, key, phase=ProviderPhase.cancelled)

    def _open_question(self, job: _Job, asked: Question) -> _Pending:
        with self._lock:
            self._questions += 1
            key = job.current or job.scope
            pending = _Pending(replace(asked, number=self._questions, provider_key=key))
            job.pending = pending
            job.notice = None
            if job.cancel.is_set():
                pending.event.set()  # cancelled already: the question returns at once
            _update(job, key, phase=ProviderPhase.waiting)
            return pending

    def _show(self, job: _Job, text: str, image: str | None) -> None:
        with self._lock:
            key = job.current or job.scope
            if not text or job.cancel.is_set():
                if job.notice is not None:
                    job.notice = None
                    _update(job, key, phase=ProviderPhase.running)
                return
            self._questions += 1
            job.notice = Notice(self._questions, key, text, image)
            _update(job, key, phase=ProviderPhase.waiting)

    def _close_question(self, job: _Job, pending: _Pending) -> str:
        with self._lock:
            if job.pending is pending:
                job.pending = None
                _update(job, pending.question.provider_key, phase=ProviderPhase.running)
            return pending.answer or ""


def _update(job: _Job, key: str, **changes: object) -> None:
    # A provider added after the job started has no line yet.
    current = job.providers.get(key) or ProviderProgress(key, ProviderPhase.queued)
    job.providers[key] = replace(current, **changes)


def _outcome_fields(outcome: ProviderSync) -> dict[str, object]:
    """The provider's line once its sync has ended, from the outcome sync_all or sync_one
    recorded for it."""
    if outcome.result is None:
        return {
            "phase": ProviderPhase.failed,
            "message": _safe(outcome.error or "the sync did not run"),
        }
    result = outcome.result
    fields = _result_fields(result)
    if result.status is SyncStatus.succeeded:
        message = result.message if result.kind is ProviderKind.receipt else None
        return {"phase": ProviderPhase.succeeded, "message": message, **fields}
    if result.status is SyncStatus.cancelled:
        return {"phase": ProviderPhase.cancelled, "message": None, **fields}
    if result.status is SyncStatus.needs_user_action:
        phase = ProviderPhase.needs_action
        fallback = "it needs you; sync again"
    else:
        phase = ProviderPhase.failed
        fallback = "the sync failed"
    return {"phase": phase, "message": _safe(result.message or fallback), **fields}


def _result_fields(result: SyncResult) -> dict[str, object]:
    report: ImportReport | None = result.report
    files = report.files if report is not None else ()
    return {
        "items_fetched": result.items_fetched,
        "created": sum(item.created for item in files),
        "joined": sum(item.joined for item in files),
        "review": sum(item.review for item in files),
        "auto_linked": 0 if report is None else report.auto.linked,
        "linked": result.linked,
        "unlinked": result.unlinked,
    }


def _safe(message: str) -> str:
    return mask_account_numbers(message)
