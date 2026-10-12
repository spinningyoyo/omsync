"""Syncs started from the Bank sync page. Each job runs in a background thread of the server
process, one job at a time, and shares the sync lock with the CLI. When a connector asks a
question, such as for an MFA code, the question waits on the page for an answer, or for a timeout.

Job state lives in memory and is lost when the server restarts; the sync log keeps the outcome of
every sync. An answer is handed to the connector and is never stored, logged, or shown again.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.discovery import mask_account_numbers
from omsync.ingest.pipeline import ImportReport
from omsync.ingest.sync import ProviderSync, SyncBusy, SyncResult, sync_all, sync_provider
from omsync.ledger.preferences import close_browser_on_failure
from omsync.ledger.sync_models import SyncStatus

# The scope of a sync of every enabled provider. Not a valid provider key, so it cannot collide.
ALL = "*"
PROMPT_TIMEOUT_SECONDS = 600.0
ANSWER_LIMIT = 200
BUSY_MESSAGE = "Another sync is running in a terminal. Try again when it finishes."
ALREADY_RUNNING = "A sync is already running. Wait for it to finish."
NO_QUESTION = "No question is waiting for an answer."
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


@dataclass(frozen=True)
class Question:
    number: int
    provider_key: str
    text: str
    secret: bool
    digits: int | None  # the digits of a one-time code, so the page can send it once complete


@dataclass(frozen=True)
class ProviderProgress:
    key: str
    phase: ProviderPhase
    message: str | None = None
    files_fetched: int = 0
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


@dataclass
class _Pending:
    question: Question
    event: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


@dataclass
class _Job:
    scope: str
    providers: dict[str, ProviderProgress]
    phase: JobPhase = JobPhase.running
    current: str | None = None
    pending: _Pending | None = None


class WebPrompt:
    """The Prompt of a web sync. ask() puts the question on the page and blocks the worker until
    it is answered or the timeout passes. A timeout returns "", which a connector reports as no
    code entered, so the provider ends as needing you."""

    def __init__(self, runner: "SyncRunner", job: _Job, timeout: float) -> None:
        self._runner = runner
        self._job = job
        self._timeout = timeout

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        pending = self._runner._open_question(self._job, question, secret, digits)
        pending.event.wait(self._timeout)
        return self._runner._close_question(self._job, pending)


class SyncRunner:
    """Runs syncs in the background and keeps the state of the current or last one. Only one job
    runs at a time; starting another while one runs is refused."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        vault: Vault,
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
        self._questions = 0

    def start(self, scope: str, provider_keys: list[str]) -> None:
        """Start a job that syncs the providers, in this order, as scope (ALL or one key). Returns
        at once; the job's state shows its progress. Raises SyncRefused while a job runs."""
        with self._lock:
            if self._job is not None and self._job.phase is JobPhase.running:
                raise SyncRefused(ALREADY_RUNNING)
            job = _Job(
                scope=scope,
                providers={
                    key: ProviderProgress(key, ProviderPhase.queued) for key in provider_keys
                },
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

    def view(self) -> JobView:
        with self._lock:
            job = self._job
            if job is None:
                return JobView(scope="", phase=JobPhase.idle, providers=(), question=None)
            pending = self._waiting_question()
            return JobView(
                scope=job.scope,
                phase=job.phase,
                providers=tuple(job.providers.values()),
                question=pending.question if pending is not None else None,
            )

    def _waiting_question(self) -> _Pending | None:
        """The unanswered question of the running job, if any. Call with the lock held."""
        job = self._job
        if job is None or job.pending is None or job.pending.answer is not None:
            return None
        return job.pending

    def _work(self, job: _Job) -> None:
        session: Session | None = None
        try:
            session = self._session_factory()
            prompt = WebPrompt(self, job, self._prompt_timeout)
            close_on_failure = close_browser_on_failure(session)
            if job.scope == ALL:
                sync_all(
                    session,
                    self._settings,
                    vault=self._vault,
                    prompt=prompt,
                    close_on_failure=close_on_failure,
                    on_progress=lambda key, outcome: self._progress(job, key, outcome),
                )
            else:
                self._progress(job, job.scope, None)
                result = sync_provider(
                    session,
                    self._settings,
                    provider_key=job.scope,
                    vault=self._vault,
                    prompt=prompt,
                    close_on_failure=close_on_failure,
                )
                self._progress(job, job.scope, ProviderSync(job.scope, result, None))
        except SyncBusy:
            self._fail_unfinished(job, BUSY_MESSAGE)
        except ConnectorError as exc:
            self._fail_unfinished(job, str(exc))
        except Exception as exc:  # the worker must never take the server down
            self._fail_unfinished(job, f"unexpected error ({type(exc).__name__})")
        finally:
            if session is not None:
                session.close()
            with self._lock:
                job.phase = JobPhase.finished
                job.pending = None
                job.current = None

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

    def _open_question(self, job: _Job, text: str, secret: bool, digits: int | None) -> _Pending:
        with self._lock:
            self._questions += 1
            key = job.current or job.scope
            pending = _Pending(Question(self._questions, key, text, secret, digits))
            job.pending = pending
            _update(job, key, phase=ProviderPhase.waiting)
            return pending

    def _close_question(self, job: _Job, pending: _Pending) -> str:
        with self._lock:
            if job.pending is pending:
                job.pending = None
                _update(job, pending.question.provider_key, phase=ProviderPhase.running)
            return pending.answer or ""


def _update(job: _Job, key: str, **changes: object) -> None:
    job.providers[key] = replace(job.providers[key], **changes)


def _outcome_fields(outcome: ProviderSync) -> dict[str, object]:
    """The provider's line once its sync has ended, from the outcome sync_all or sync_provider
    recorded for it."""
    if outcome.result is None:
        return {
            "phase": ProviderPhase.failed,
            "message": _safe(outcome.error or "the sync did not run"),
        }
    result = outcome.result
    fields = _result_fields(result)
    if result.status is SyncStatus.succeeded:
        return {"phase": ProviderPhase.succeeded, "message": None, **fields}
    if result.status is SyncStatus.needs_user_action:
        phase = ProviderPhase.needs_action
        fallback = "the bank needs you; sync again"
    else:
        phase = ProviderPhase.failed
        fallback = "the sync failed"
    return {"phase": phase, "message": _safe(result.message or fallback), **fields}


def _result_fields(result: SyncResult) -> dict[str, object]:
    report: ImportReport | None = result.report
    files = report.files if report is not None else ()
    return {
        "files_fetched": result.files_fetched,
        "created": sum(item.created for item in files),
        "joined": sum(item.joined for item in files),
        "review": sum(item.review for item in files),
        "auto_linked": 0 if report is None else report.auto.linked,
        "linked": result.linked,
        "unlinked": result.unlinked,
    }


def _safe(message: str) -> str:
    return mask_account_numbers(message)
