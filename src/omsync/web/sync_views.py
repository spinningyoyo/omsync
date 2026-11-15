"""View data for the Bank sync status region: its headline, one line per provider, and the question
waiting for an answer or the notice shown in its place. Nothing here reads the database or the
vault."""

from dataclasses import dataclass

from omsync.web.sync_runner import (
    ALL,
    JobPhase,
    JobView,
    Notice,
    ProviderPhase,
    ProviderProgress,
    Question,
)

LABELS: dict[ProviderPhase, str] = {
    ProviderPhase.queued: "Queued",
    ProviderPhase.running: "Running",
    ProviderPhase.waiting: "Waiting for you",
    ProviderPhase.succeeded: "Succeeded",
    ProviderPhase.failed: "Failed",
    ProviderPhase.needs_action: "Needs you",
    ProviderPhase.cancelled: "Cancelled",
}
TONES: dict[ProviderPhase, str] = {
    ProviderPhase.queued: "text-secondary",
    ProviderPhase.running: "text-primary",
    ProviderPhase.waiting: "text-primary",
    ProviderPhase.succeeded: "text-success",
    ProviderPhase.failed: "text-destructive",
    ProviderPhase.needs_action: "text-destructive",
    ProviderPhase.cancelled: "text-secondary",
}


@dataclass(frozen=True)
class ProviderLineView:
    key: str
    label: str
    detail: str
    tone: str
    link: bool  # statement accounts are unlinked, so the person can link them now


@dataclass(frozen=True)
class SyncStatusView:
    idle: bool  # no job has run since omsync started, so there is nothing to report
    running: bool
    cancelling: bool
    headline: str
    lines: list[ProviderLineView]
    question: Question | None
    notice: Notice | None
    shown_number: int  # the question's or notice's, 0 for neither; a poll reports what it shows


def sync_status_view(view: JobView) -> SyncStatusView:
    shown = view.question or view.notice
    return SyncStatusView(
        idle=view.phase is JobPhase.idle,
        running=view.phase is JobPhase.running,
        cancelling=view.cancelling,
        headline=_headline(view),
        lines=[_line(progress) for progress in view.providers],
        question=view.question,
        notice=view.notice,
        shown_number=shown.number if shown is not None else 0,
    )


def _headline(view: JobView) -> str:
    if view.phase is JobPhase.idle:
        return "No sync has run from this page yet."
    if view.cancelling:
        return "Cancelling. The bank in progress stops at its next step."
    if view.phase is JobPhase.running:
        target = "every enabled provider" if view.scope == ALL else view.scope
        return f"Syncing {target}."
    ended = (
        "Cancelled."
        if any(item.phase is ProviderPhase.cancelled for item in view.providers)
        else "Finished."
    )
    if view.scope != ALL:
        return ended
    succeeded = sum(1 for item in view.providers if item.phase is ProviderPhase.succeeded)
    return f"{ended} {succeeded} of {len(view.providers)} succeeded."


def _line(progress: ProviderProgress) -> ProviderLineView:
    return ProviderLineView(
        key=progress.key,
        label=LABELS[progress.phase],
        detail=_detail(progress),
        tone=TONES[progress.phase],
        link=bool(progress.unlinked),
    )


def _detail(progress: ProviderProgress) -> str:
    if progress.phase is ProviderPhase.succeeded:
        files = "1 file" if progress.files_fetched == 1 else f"{progress.files_fetched} files"
        detail = (
            f"{files} fetched. Imported: created {progress.created}, "
            f"joined {progress.joined}, review {progress.review}."
        )
        if progress.auto_linked:
            pairs = "1 pair" if progress.auto_linked == 1 else f"{progress.auto_linked} pairs"
            detail += f" Linked {pairs} automatically."
        if progress.linked:
            digits = ", ".join(f"••{last4}" for last4 in progress.linked)
            detail += f" Linked {digits} to the accounts that end the same."
        return detail
    if progress.phase in (ProviderPhase.failed, ProviderPhase.needs_action):
        message = progress.message or ""
        if progress.created or progress.joined:
            message += f" Imported the rest: created {progress.created}, joined {progress.joined}."
        return message
    if progress.phase is ProviderPhase.waiting:
        return "See below."
    return ""
