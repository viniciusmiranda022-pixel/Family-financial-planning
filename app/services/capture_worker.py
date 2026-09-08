"""Durable DB-backed queue for asynchronous OCR/audio capture processing.

Fase 3 item 1 (`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md`): moves the
potentially slow OCR (scanned image/PDF) and Whisper transcription work off
the `POST /captures/preview` request path without a second processing
engine, a message broker, or any authority over financial facts. This
module only manages `CaptureProcessingJob` rows (claim/complete/fail/retry
bookkeeping); the actual OCR/Whisper/classification work is still done by
the existing canonical `app.services.smart_capture` functions, invoked from
the single shared pipeline in `app.api._analyze_and_persist_capture`.

Deliberately does not use `SELECT ... FOR UPDATE SKIP LOCKED`. This project
runs a single PostgreSQL instance for one family/household deployment (see
`docs/ARCHITECTURE.md`'s MVP note and `docs/INTEGRITY_IMPLEMENTATION_PLAN.md`
section 15: "a modelagem deve permitir worker futuro sem obrigar Redis
nesta etapa"), and an atomic `UPDATE ... WHERE status = ...` compare-and-swap
claim is dialect-agnostic -- it behaves identically on PostgreSQL
(production) and SQLite (tests), which matters more here than the extra
concurrency headroom `SKIP LOCKED` buys for a household-scale workload.
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CaptureDraft, CaptureProcessingJob, Document

AUDIO_SUFFIXES = {".webm", ".m4a", ".mp3", ".wav", ".ogg"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".heic"}

TERMINAL_STATUSES = {"completed", "failed"}


def _aware_utc(value: datetime | None) -> datetime | None:
    """Normalize a `DateTime(timezone=True)` column value read back from the
    database for a Python-level comparison against `datetime.now(UTC)`.

    PostgreSQL always returns a timezone-aware value for this column type;
    SQLite (used in tests) always returns a naive one regardless -- every
    value this app ever writes to such a column is already UTC
    (`datetime.now(UTC)`), so a naive value is safely assumed to already be
    UTC rather than the local clock. Only Python-level comparisons need
    this: SQL-level `WHERE ... < :cutoff` comparisons compare correctly on
    both dialects without it, since SQLAlchemy's bind/result processors
    already serialize consistently within one dialect.
    """

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def classify_async_job_type(
    filename: str | None, content_type: str | None, *, has_payload: bool
) -> str | None:
    """Return `"audio"`, `"ocr"`, or `None` (fast/synchronous path).

    Mirrors -- deliberately, not by re-implementing -- the exact
    audio/image/pdf dispatch `app.services.smart_capture.preview_capture`
    and `extract_document_text` already use, so the decision "does this
    upload need the async queue" can never drift from the decision those
    functions make about *how* to process it. Plain text and fast
    structured formats (CSV/OFX/TXT) return `None`: Fase 3 item 1 is scoped
    to OCR and audio only (item 9, "não antecipar" other roadmap items).
    """

    if not has_payload:
        return None
    content_type = content_type or ""
    suffix = Path(filename or "").suffix.lower()
    if content_type.startswith("audio/") or suffix in AUDIO_SUFFIXES:
        return "audio"
    if content_type.startswith("image/") or suffix in IMAGE_SUFFIXES:
        return "ocr"
    if suffix == ".pdf" or content_type == "application/pdf":
        return "ocr"
    return None


def worker_identity() -> str:
    """Best-effort, non-sensitive label for `claimed_by` -- observability
    only, never used for authorization or as a lock token."""

    return f"{socket.gethostname()}:{os.getpid()}"


def create_capture_job(
    db: Session,
    *,
    household_id: str,
    capture_draft_id: str,
    document_id: str | None,
    job_type: str,
    content_type: str | None,
    max_attempts: int,
) -> CaptureProcessingJob:
    job = CaptureProcessingJob(
        household_id=household_id,
        capture_draft_id=capture_draft_id,
        document_id=document_id,
        job_type=job_type,
        content_type=(content_type or "")[:100] or None,
        status="queued",
        max_attempts=max_attempts,
    )
    db.add(job)
    db.flush()
    return job


def claim_capture_job(
    db: Session,
    job_id: str,
    *,
    worker_id: str,
    stale_after_seconds: int,
) -> CaptureProcessingJob | None:
    """Atomically claim `job_id` if it is `queued`, or if it is `processing`
    but has been so for longer than `stale_after_seconds` (its previous
    worker crashed or was killed mid-job -- item 5, "recuperação de
    worker").

    Uses one `UPDATE ... WHERE id = :id AND status = :observed_status AND
    attempts = :observed_attempts` so two callers racing on the same job
    (the request's own background task and a concurrently-run reconciler
    sweep, or two reconciler sweeps) cannot both believe they claimed it.

    `status` alone is not a sufficient compare-and-swap predicate for the
    *reclaim* transition: it goes `processing` -> `processing` (only
    `attempts`/`started_at`/`claimed_by` change), so a second reclaimer
    that observed the same stale row before the first one committed would
    still match `WHERE status = 'processing'` after that commit -- the
    persisted value never actually changed. Comparing `attempts` too closes
    this: every successful claim (initial or reclaim) increments it, so a
    second racer's `WHERE ... AND attempts = :observed_attempts` is bound
    to the pre-image it read and stops matching the instant the first
    racer's claim commits. `rowcount == 0` then tells every loser it lost
    the race, and it returns `None` rather than processing anything -- this
    is what makes duplicate delivery / concurrent retry / concurrent
    crash-recovery reclaim safe (item 4).
    """

    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    job = db.get(CaptureProcessingJob, job_id)
    if job is None or job.status in TERMINAL_STATUSES:
        return None
    started_at = _aware_utc(job.started_at)
    claimable = job.status == "queued" or (
        job.status == "processing" and (started_at is None or started_at < stale_cutoff)
    )
    if not claimable:
        return None
    observed_status = job.status
    observed_attempts = job.attempts
    result = db.execute(
        update(CaptureProcessingJob)
        .where(
            CaptureProcessingJob.id == job_id,
            CaptureProcessingJob.status == observed_status,
            CaptureProcessingJob.attempts == observed_attempts,
        )
        .values(
            status="processing",
            attempts=CaptureProcessingJob.attempts + 1,
            started_at=now,
            claimed_by=worker_id,
            error_code=None,
        )
    )
    db.commit()
    if result.rowcount == 0:
        return None
    db.refresh(job)
    return job


def finalize_capture_job(
    db: Session,
    job_id: str,
    *,
    status: str,
    claimed_attempt: int,
    error_code: str | None = None,
    duration_ms: int | None = None,
) -> bool:
    """Atomically transition `job_id` from `processing` to a terminal
    status (`"completed"` or `"failed"`), succeeding only if it is still
    the same `processing` *attempt* this call is finalizing -- `job.attempts`
    as it stood right after `claim_capture_job` returned it, i.e. this
    worker's lease version for the attempt it is finishing. Returns whether
    this call actually won that transition.

    Closes the cancellation-vs-worker race flagged in review of this Work
    Order: `app.api.cancel_capture` can move a job straight from
    `processing` to `failed` (`error_code="cancelled_by_user"`), in its own
    independently committed transaction, while a worker further along is
    still mid-flight on that same job. Without this guard, a worker that
    started before the cancellation could finish afterwards and
    unconditionally overwrite that terminal state -- resurrecting a
    cancelled capture with a freshly written proposal, or silently
    replacing `"cancelled_by_user"` with a processing error.

    Also closes a second, later-found race: `claim_capture_job`'s CAS on
    `(status, attempts)` stops two *reclaimers* from both winning the same
    stale pre-image, but `status` alone stopped there is not enough to stop
    an *old* lease from finalizing a *newer* one. If worker A claims
    attempt 1 and stalls past `stale_after_seconds`, worker B reclaims the
    same job (bumping `attempts` to 2, `status` staying `"processing"`),
    and A then finishes and calls this function -- a `WHERE status =
    'processing'` predicate alone would still match, letting A publish
    attempt 1's stale result (or terminal state) over attempt 2's lease.
    Comparing `attempts == claimed_attempt` too closes this exactly like
    `claim_capture_job` closes the reclaim race: A's `claimed_attempt` is
    pinned to `1`, which no longer matches the persisted `2` the instant
    B's reclaim commits, so A's finalize loses (`rowcount == 0`) and its
    caller must discard everything else it staged, same as the
    cancellation race below.

    Reuses the same compare-and-swap discipline as `claim_capture_job`:
    `UPDATE ... WHERE status = 'processing' AND attempts = :claimed_attempt`
    only ever affects the row if nothing else has already resolved or
    superseded this exact attempt, so `rowcount == 0` unambiguously means
    some other transaction (a cancellation, or a newer lease) already
    finalized/superseded this job first. The caller (see
    `app.api._process_claimed_capture_job`) must then discard --
    `db.rollback()` -- everything else it staged in this transaction (the
    capture/document mutations `_analyze_and_persist_capture` made)
    instead of committing a finalize that lost the race alongside them.
    """

    values: dict[str, object] = {
        "status": status,
        "completed_at": datetime.now(UTC),
        "error_code": (error_code[:80] if error_code else None),
    }
    if duration_ms is not None:
        values["duration_ms"] = max(0, duration_ms)
    result = db.execute(
        update(CaptureProcessingJob)
        .where(
            CaptureProcessingJob.id == job_id,
            CaptureProcessingJob.status == "processing",
            CaptureProcessingJob.attempts == claimed_attempt,
        )
        .values(**values)
    )
    return result.rowcount == 1


def mark_job_completed(db: Session, job: CaptureProcessingJob, *, duration_ms: int | None = None) -> None:
    """`duration_ms` is measured by the caller with `time.perf_counter()`
    around the actual processing call (like every other duration this
    codebase records -- see `app.cli.backfill`/`app.services.financial_integrity`),
    not by subtracting `job.started_at` from now: SQLite (tests) returns
    `DateTime(timezone=True)` columns as timezone-*naive*, which cannot be
    subtracted from an aware `datetime.now(UTC)`, and even on PostgreSQL a
    reclaimed job's `started_at` reflects the *current* claim, not
    necessarily when this specific attempt's measured work began. `None`
    (left unset) is honest when no such measurement is available -- e.g. a
    crash-recovery job whose original attempt never finished timing itself
    (see `fail_exhausted_stale_jobs`)."""

    job.status = "completed"
    job.completed_at = datetime.now(UTC)
    if duration_ms is not None:
        job.duration_ms = max(0, duration_ms)
    job.error_code = None


def mark_job_failed(
    db: Session, job: CaptureProcessingJob, *, error_code: str, duration_ms: int | None = None
) -> None:
    """Terminal failure: fail-closed, never promotes an incomplete result to
    `completed`. Never deletes or rewrites the original document/draft --
    a household member can retry via `POST /captures/{id}/retry`
    (`app.api.retry_capture`), which is the only thing that moves a
    `failed` job back to `queued`. See `mark_job_completed` for why
    `duration_ms` is caller-measured rather than derived from
    `started_at`."""

    job.status = "failed"
    job.completed_at = datetime.now(UTC)
    if duration_ms is not None:
        job.duration_ms = max(0, duration_ms)
    job.error_code = error_code[:80]


def reset_job_for_retry(db: Session, job: CaptureProcessingJob) -> None:
    """Manual retry (`POST /captures/{id}/retry`) is a deliberate household
    action, so -- unlike the automatic reconciler reclaim below -- it is
    not bounded by `max_attempts`; `attempts` itself is left untouched
    (cumulative audit history of how many times this job has ever run),
    only the terminal/clock fields reset so it looks freshly queued."""

    job.status = "queued"
    job.error_code = None
    job.started_at = None
    job.completed_at = None
    job.duration_ms = None
    job.claimed_by = None


def find_claimable_job_ids(db: Session, *, limit: int, stale_after_seconds: int) -> list[str]:
    """Used by the crash-recovery reconciler (`app/cli/capture_worker.py`)
    to discover work: any `queued` job, plus any `processing` job stuck
    past `stale_after_seconds` (its worker died) that still has attempts
    left. A stale job that has exhausted `max_attempts` is deliberately
    excluded here -- see `fail_exhausted_stale_jobs`, which fails it
    outright instead of offering it for endless reclaim."""

    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    rows = db.scalars(
        select(CaptureProcessingJob.id)
        .where(
            (CaptureProcessingJob.status == "queued")
            | (
                (CaptureProcessingJob.status == "processing")
                & (CaptureProcessingJob.started_at.is_not(None))
                & (CaptureProcessingJob.started_at < stale_cutoff)
                & (CaptureProcessingJob.attempts < CaptureProcessingJob.max_attempts)
            )
        )
        .order_by(CaptureProcessingJob.created_at)
        .limit(limit)
    ).all()
    return list(rows)


def fail_exhausted_stale_jobs(db: Session, *, stale_after_seconds: int) -> int:
    """Explicitly fail any `processing` job stuck past `stale_after_seconds`
    that has already used all of its attempts, so it never sits in
    `processing` forever misrepresenting an in-flight worker (fail-closed,
    item 5). This is the terminal counterpart to `find_claimable_job_ids`,
    which stops offering such a job for reclaim once attempts are
    exhausted. Also marks the correlated `CaptureDraft`/`Document` honestly
    (unless the draft was already confirmed or cancelled by the household
    in the meantime, in which case its own terminal state stands).

    Reuses `finalize_capture_job`'s `(status='processing', attempts=
    :claimed_attempt)` compare-and-swap instead of mutating a loaded ORM
    row (which would UPDATE by primary key alone, with no predicate on the
    lease it observed). Without that CAS, a real interleaving is provable:
    this function reads a job as stale/exhausted, but before it commits,
    the job's *actual* owning worker (still alive, just slow) finishes and
    calls `finalize_capture_job` with the attempt it legitimately claimed
    -- winning cleanly because nothing else has touched that row yet. This
    function's later, unconditional `UPDATE ... WHERE id = :id` would then
    overwrite that valid `completed` terminal state (and the draft/document
    it wrote) back to `failed`, exactly the asymmetry the CAS on
    `claim_capture_job`/`finalize_capture_job` already closes for every
    other transition. Passing the `attempts` value observed in the same
    query that found this job stale as `claimed_attempt` makes the two
    functions share one lease: whichever transaction's UPDATE actually
    commits first -- this reconciler failing the exhausted attempt, or the
    slow worker finalizing it -- wins the row lock, and the other one's CAS
    predicate no longer matches once it runs, so it loses (`False`) instead
    of clobbering the winner. A job that loses this race is left completely
    untouched (job, draft and document alike) and is not counted as failed
    here; the reconciler simply leaves whatever the winner wrote standing.

    Every draft/document lookup here is scoped by the job's `household_id`
    (never a bare `db.get(Model, id)` by primary key alone), exactly like
    `app.api._process_claimed_capture_job` -- a job whose FKs have drifted
    to point at another household's draft/document (corrupted/inconsistent
    persisted state) must never have that foreign row loaded or mutated
    just because its own attempt budget ran out. A job that fails this
    correlation simply leaves the foreign draft/document untouched; only
    its own `capture_processing_jobs` row is failed above. These lookups
    only run for a job whose CAS above actually won, so a lease this
    function lost can never reach them either.

    Caller is responsible for `db.commit()`.
    """

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=max(1, stale_after_seconds))
    stuck = db.execute(
        select(
            CaptureProcessingJob.id,
            CaptureProcessingJob.household_id,
            CaptureProcessingJob.capture_draft_id,
            CaptureProcessingJob.document_id,
            CaptureProcessingJob.attempts,
        ).where(
            CaptureProcessingJob.status == "processing",
            CaptureProcessingJob.started_at.is_not(None),
            CaptureProcessingJob.started_at < cutoff,
            CaptureProcessingJob.attempts >= CaptureProcessingJob.max_attempts,
        )
    ).all()
    failed_count = 0
    for job_id, household_id, capture_draft_id, document_id, observed_attempts in stuck:
        won = finalize_capture_job(
            db,
            job_id,
            status="failed",
            claimed_attempt=observed_attempts,
            error_code="worker_timeout_exhausted",
        )
        if not won:
            continue
        failed_count += 1
        # `finalize_capture_job` issues a Core `UPDATE`, which -- unlike a
        # direct ORM attribute assignment -- does not sync any instance of
        # this row already tracked in `db`'s identity map (e.g. a caller
        # that loaded it earlier in the same session). `populate_existing`
        # forces a fresh read of the just-applied-but-not-yet-committed
        # write (still visible to this same transaction) into that
        # instance, so nothing downstream in this session can observe a
        # stale `status`/`error_code` for a job this call just won.
        db.get(CaptureProcessingJob, job_id, populate_existing=True)
        draft = db.scalar(
            select(CaptureDraft).where(
                CaptureDraft.id == capture_draft_id, CaptureDraft.household_id == household_id
            )
        )
        if draft is not None and draft.status not in {"confirmed", "cancelled"}:
            draft.status = "failed"
        if document_id:
            document = db.scalar(
                select(Document).where(
                    Document.id == document_id, Document.household_id == household_id
                )
            )
            if document is not None:
                document.status = "capture_failed"
    return failed_count


__all__ = [
    "classify_async_job_type",
    "worker_identity",
    "create_capture_job",
    "claim_capture_job",
    "finalize_capture_job",
    "mark_job_completed",
    "mark_job_failed",
    "reset_job_for_retry",
    "find_claimable_job_ids",
    "fail_exhausted_stale_jobs",
]
