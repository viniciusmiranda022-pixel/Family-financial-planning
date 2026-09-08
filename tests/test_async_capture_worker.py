"""Tests for the Fase 3 async OCR/audio capture worker
(`docs/WORK_ORDER_ASYNC_OCR_AUDIO_WORKER.md`).

Exercises `POST /captures/preview` end to end for image and audio uploads
against the real FastAPI app. OCR/Whisper themselves are monkeypatched at
`app.services.smart_capture.extract_document_text`/`transcribe_audio` --
the exact functions `preview_capture` calls -- so these tests are
deterministic and do not require Tesseract/faster-whisper installed;
everything else (classification, queueing, atomic claiming, the shared
analysis/persistence pipeline, retry, crash recovery, household isolation,
encryption-at-rest, human confirmation still required) runs for real.

`app.api._run_capture_job_background` and
`app.cli.capture_worker.reconcile_once` always open their own DB session
(they run outside any request, so there is no request-scoped session to
reuse) -- tests inject the same isolated `session_factory` these tests use
for everything else via `app.dependency_overrides[get_capture_job_session_factory]`,
exactly like `get_db` is overridden, rather than relying on which test
module happens to import `app.db` first in the shared pytest process (see
`app.api.get_capture_job_session_factory`'s docstring).

`TestClient`/Starlette run `BackgroundTasks` synchronously as part of
handling each request (inside `Response.__call__`, before the ASGI call
returns), so by the time a `client.post(...)` call returns here, a queued
job has already been claimed and processed to completion -- this is also
true in production for any single request, just more commonly interleaved
with other concurrent requests there.

Uses the same isolated-engine + TestClient + real-login pattern as
`tests/test_batch_import.py`.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SECRET_KEY", "async-capture-worker-test-secret-that-is-long-enough-aaaa")
os.environ.setdefault("FILE_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATA_DIR", f"/tmp/ffp-async-capture-data-{uuid.uuid4().hex}")

import app.api as api_module  # noqa: E402
import app.services.smart_capture as smart_capture_module  # noqa: E402
from app.cli.capture_worker import reconcile_once  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    CaptureDraft,
    CaptureProcessingJob,
    Document,
    Household,
    Transaction,
    User,
)
from app.security import hash_password  # noqa: E402
from app.services import capture_worker  # noqa: E402

_test_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
_TestSessionLocal = sessionmaker(bind=_test_engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(bind=_test_engine)


def _override_get_db():
    db = _TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _install_overrides() -> None:
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[api_module.get_capture_job_session_factory] = lambda: _TestSessionLocal


def _client() -> TestClient:
    _install_overrides()
    return TestClient(app)


def _setup_household(client: TestClient, *, household_name: str, username: str, password: str) -> dict:
    with _TestSessionLocal() as db:
        household = Household(name=household_name)
        db.add(household)
        db.flush()
        db.add(
            User(
                household_id=household.id,
                name="Administrador",
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
        household_id = household.id
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"household_id": household_id, "username": username}


def _create_account(client: TestClient, **overrides) -> dict:
    payload = {
        "name": "Conta Corrente",
        "institution": "Itaú",
        "account_type": "checking",
        "owner_label": "Família",
    }
    payload.update(overrides)
    response = client.post("/api/accounts", json=payload)
    assert response.status_code == 201
    return response.json()


def _fake_ocr(monkeypatch, *, text: str = "Gastei R$ 45,00 no mercado hoje", processor: str = "ocr_local") -> None:
    monkeypatch.setattr(
        smart_capture_module,
        "extract_document_text",
        lambda filename, payload, content_type: (text, processor),
    )


def _fake_audio(monkeypatch, *, text: str = "Gastei R$ 45,00 no mercado hoje") -> None:
    monkeypatch.setattr(smart_capture_module, "transcribe_audio", lambda filename, payload: text)


def _png_bytes() -> bytes:
    # A PNG-signature-prefixed blob so EncryptedDocumentStore has real
    # bytes to encrypt/decrypt and the "not stored in plaintext" test has a
    # recognizable magic header to look for; extract_document_text is
    # monkeypatched, so these bytes are never actually decoded as an image.
    return b"\x89PNG\r\n\x1a\n" + b"fake-image-body-bytes-not-a-real-png"


def test_image_upload_is_queued_then_processed_into_a_preview(monkeypatch) -> None:
    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família OCR", username="admin-ocr-1", password="senha-ocr-segura-1")
            _create_account(client)
            response = client.post(
                "/api/captures/preview", files={"file": ("recibo.png", _png_bytes(), "image/png")}
            )
            assert response.status_code == 201
            queued = response.json()
            # The POST's own response body is built (and its JSON already
            # serialized) *before* Starlette runs the queued BackgroundTask,
            # so it always reports "queued" here -- exactly what a real
            # client sees and why the UI polls GET /captures/{id}
            # afterwards. By the time this call returns at all, though, the
            # background task has already run against the database.
            assert queued["status"] == "queued"
            assert queued["source_type"] == "image"
            assert queued["job"]["job_type"] == "ocr"
            assert queued["job"]["status"] == "queued"

            capture = client.get(f"/api/captures/{queued['id']}").json()
            assert capture["status"] == "preview"
            assert capture["job"]["status"] == "completed"
            assert capture["job"]["attempts"] == 1
            assert len(capture["items"]) == 1
            assert capture["items"][0]["amount"] == 45.0

        with _TestSessionLocal() as db:
            draft = db.get(CaptureDraft, capture["id"])
            job = db.scalar(select(CaptureProcessingJob).where(CaptureProcessingJob.capture_draft_id == draft.id))
            document = db.get(Document, draft.document_id)
            assert job.capture_draft_id == draft.id
            assert job.household_id == draft.household_id == document.household_id
            assert job.document_id == document.id
            assert job.status == "completed"
            assert job.duration_ms is not None
            # No Transaction exists yet -- OCR/worker completion is only a
            # proposal; human confirmation is still required.
            assert db.scalar(select(Transaction).where(Transaction.household_id == draft.household_id)) is None
    finally:
        app.dependency_overrides.clear()


def test_audio_upload_is_queued_then_transcribed_into_a_preview(monkeypatch) -> None:
    _fake_audio(monkeypatch)
    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família Áudio", username="admin-audio-1", password="senha-audio-segura-1")
            _create_account(client)
            response = client.post(
                "/api/captures/preview",
                files={"file": ("lancamento.webm", b"fake-audio-bytes", "audio/webm")},
            )
            assert response.status_code == 201
            queued = response.json()
            assert queued["status"] == "queued"
            assert queued["source_type"] == "audio"
            assert queued["job"]["job_type"] == "audio"

            capture = client.get(f"/api/captures/{queued['id']}").json()
            assert capture["status"] == "preview"
            assert capture["job"]["status"] == "completed"
            assert capture["processor"] == "whisper_local"
    finally:
        app.dependency_overrides.clear()


def test_plain_text_capture_is_not_queued_and_stays_synchronous(monkeypatch) -> None:
    """Fase 3 item 1 scope is OCR/audio only -- CSV/OFX/plain-text capture
    must not go through the async queue (item 9: don't anticipate other
    roadmap items, and no regression of the existing fast path)."""

    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família Texto", username="admin-texto-1", password="senha-texto-segura-1")
            _create_account(client)
            response = client.post("/api/captures/preview", data={"text": "Gastei R$ 30 no mercado hoje"})
            assert response.status_code == 201
            capture = response.json()
            assert capture["status"] == "preview"
            assert capture["job"] is None

        with _TestSessionLocal() as db:
            draft = db.get(CaptureDraft, capture["id"])
            assert (
                db.scalar(
                    select(CaptureProcessingJob).where(CaptureProcessingJob.capture_draft_id == draft.id)
                )
                is None
            )
    finally:
        app.dependency_overrides.clear()


def test_sync_fallback_toggle_processes_image_inline_without_a_job(monkeypatch) -> None:
    """Item 8: rollout compatibility switch -- with async processing
    disabled, an image upload is processed exactly like before this slice
    (no `CaptureProcessingJob`, immediate `preview`)."""

    _fake_ocr(monkeypatch)
    monkeypatch.setattr(api_module.settings, "capture_async_processing_enabled", False)
    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família Rollback", username="admin-rollback-1", password="senha-rollback-segura-1")
            _create_account(client)
            response = client.post(
                "/api/captures/preview", files={"file": ("recibo.png", _png_bytes(), "image/png")}
            )
            assert response.status_code == 201
            capture = response.json()
            assert capture["status"] == "preview"
            assert capture["job"] is None

        with _TestSessionLocal() as db:
            draft = db.get(CaptureDraft, capture["id"])
            assert (
                db.scalar(
                    select(CaptureProcessingJob).where(CaptureProcessingJob.capture_draft_id == draft.id)
                )
                is None
            )
    finally:
        app.dependency_overrides.clear()


def test_processing_failure_is_recorded_honestly_and_retry_reprocesses_in_place(monkeypatch) -> None:
    """Items 4-5: a genuine worker failure never gets silently promoted to
    success, and the only way to move past `failed` is the explicit
    `/retry` endpoint -- which must re-process the *same* draft/document,
    never create new ones."""

    def _boom(filename, payload, content_type):
        raise RuntimeError("simulated OCR crash")

    monkeypatch.setattr(smart_capture_module, "extract_document_text", _boom)
    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família Falha", username="admin-falha-1", password="senha-falha-segura-1")
            _create_account(client)
            response = client.post(
                "/api/captures/preview", files={"file": ("recibo.png", _png_bytes(), "image/png")}
            )
            assert response.status_code == 201
            capture_id = response.json()["id"]
            capture = client.get(f"/api/captures/{capture_id}").json()
            assert capture["status"] == "failed"
            assert capture["job"]["status"] == "failed"
            assert capture["job"]["error_code"] == "processing_error"

            with _TestSessionLocal() as db:
                assert db.scalar(select(CaptureProcessingJob).where(CaptureProcessingJob.status == "failed"))
                document_count_before = len(db.scalars(select(Document)).all())
                draft_count_before = len(db.scalars(select(CaptureDraft)).all())

            # Confirming a failed capture must be refused (a syntactically
            # valid item is required to even reach that check -- an empty
            # list is rejected by request validation first).
            confirm_response = client.post(
                f"/api/captures/{capture_id}/confirm",
                json={
                    "items": [
                        {
                            "kind": "transaction",
                            "selected": True,
                            "booked_at": "2026-01-01",
                            "description": "Item de teste",
                            "amount": 10,
                            "movement_type": "expense",
                        }
                    ],
                    "confirmed_large_amount": False,
                },
            )
            assert confirm_response.status_code == 409

            # Fix the "bug" and retry: same draft/document, now succeeds.
            _fake_ocr(monkeypatch)
            retry_response = client.post(f"/api/captures/{capture_id}/retry")
            assert retry_response.status_code == 200
            assert retry_response.json()["status"] == "queued"  # same reason as above: pre-background-task snapshot

            retried = client.get(f"/api/captures/{capture_id}").json()
            assert retried["id"] == capture_id
            assert retried["status"] == "preview"
            assert retried["job"]["status"] == "completed"
            assert retried["job"]["attempts"] == 2  # first (failed) + retry

            # A second retry is refused: the job is no longer `failed`.
            second_retry = client.post(f"/api/captures/{capture_id}/retry")
            assert second_retry.status_code == 409

        with _TestSessionLocal() as db:
            assert len(db.scalars(select(Document)).all()) == document_count_before
            assert len(db.scalars(select(CaptureDraft)).all()) == draft_count_before
            draft = db.get(CaptureDraft, capture_id)
            jobs_for_draft = db.scalars(
                select(CaptureProcessingJob).where(CaptureProcessingJob.capture_draft_id == draft.id)
            ).all()
            assert len(jobs_for_draft) == 1  # retry updated the same row, never inserted a second
    finally:
        app.dependency_overrides.clear()


def test_concurrent_claim_is_race_safe_and_never_double_processes(monkeypatch) -> None:
    """Item 4: two callers racing to claim the same job (a duplicate
    delivery, or the inline background task and a reconciler sweep
    overlapping) -- only one may win."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Corrida", username="admin-corrida-1", password="senha-corrida-segura-1"
            )["household_id"]
            _create_account(client)
            # Manually enqueue a job without letting the background task run
            # yet, by creating the rows directly (mirrors what the route does).
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = capture_worker.create_capture_job(
                    db,
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    max_attempts=3,
                )
                db.commit()
                job_id = job.id

            with _TestSessionLocal() as db_a, _TestSessionLocal() as db_b:
                claimed_a = capture_worker.claim_capture_job(
                    db_a, job_id, worker_id="worker-a", stale_after_seconds=600
                )
                claimed_b = capture_worker.claim_capture_job(
                    db_b, job_id, worker_id="worker-b", stale_after_seconds=600
                )
                assert claimed_a is not None
                assert claimed_b is None  # lost the race -- must not reprocess

            with _TestSessionLocal() as db:
                refreshed = db.get(CaptureProcessingJob, job_id)
                assert refreshed.status == "processing"
                assert refreshed.attempts == 1
                assert refreshed.claimed_by == "worker-a"
    finally:
        app.dependency_overrides.clear()


def test_crash_recovery_reconciler_reclaims_stale_processing_job(monkeypatch) -> None:
    """Item 5: a job stuck in `processing` past the stale timeout (its
    worker crashed) is reclaimed and finished by the reconciler -- the
    stand-in for `app.cli.capture_worker` run out-of-band."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Recuperação", username="admin-recovery-1", password="senha-recovery-segura-1"
            )["household_id"]
            _create_account(client)
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="processing",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                document = Document(
                    household_id=household_id,
                    original_name="recibo.png",
                    document_type="smart_capture",
                    sha256="a" * 64,
                    encrypted_path="ignored-for-this-test",
                    status="capture_processing",
                )
                db.add(document)
                db.flush()
                job = CaptureProcessingJob(
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=document.id,
                    job_type="ocr",
                    content_type="image/png",
                    status="processing",
                    attempts=1,
                    max_attempts=3,
                    started_at=datetime.now(UTC) - timedelta(seconds=900),
                )
                db.add(job)
                db.commit()
                job_id = job.id
                document_id = document.id

            # Reconciler reads the encrypted document from disk (no
            # in-memory payload survives a crash) -- write real encrypted
            # bytes for it to read.
            from app.services.crypto import EncryptedDocumentStore

            with _TestSessionLocal() as db:
                document = db.get(Document, document_id)
                document.encrypted_path = EncryptedDocumentStore().save(document.id, _png_bytes())
                db.commit()

            result = asyncio.run(
                reconcile_once(limit=10, stale_after_seconds=600, session_factory=_TestSessionLocal)
            )
            assert result["reclaimed"] >= 1

        with _TestSessionLocal() as db:
            job = db.get(CaptureProcessingJob, job_id)
            assert job.status == "completed"
            assert job.attempts == 2
            draft = db.get(CaptureDraft, job.capture_draft_id)
            assert draft.status == "preview"
    finally:
        app.dependency_overrides.clear()


def test_exhausted_stale_job_is_failed_not_left_processing_forever() -> None:
    """Item 5, fail-closed: once attempts are exhausted, a stale
    `processing` job is explicitly failed rather than offered for reclaim
    forever or silently treated as done."""

    with _TestSessionLocal() as db:
        household = Household(name="Família Esgotada")
        db.add(household)
        db.flush()
        draft = CaptureDraft(
            household_id=household.id,
            source_type="image",
            detected_type="auto",
            status="processing",
            processor="pending",
            proposal_json="[]",
        )
        db.add(draft)
        db.flush()
        job = CaptureProcessingJob(
            household_id=household.id,
            capture_draft_id=draft.id,
            document_id=None,
            job_type="ocr",
            status="processing",
            attempts=3,
            max_attempts=3,
            started_at=datetime.now(UTC) - timedelta(seconds=900),
        )
        db.add(job)
        db.commit()
        job_id, draft_id = job.id, draft.id

        assert job_id not in capture_worker.find_claimable_job_ids(db, limit=1000, stale_after_seconds=600)
        failed_count = capture_worker.fail_exhausted_stale_jobs(db, stale_after_seconds=600)
        db.commit()
        assert failed_count >= 1

        job = db.get(CaptureProcessingJob, job_id)
        draft = db.get(CaptureDraft, draft_id)
        assert job.status == "failed"
        assert job.error_code == "worker_timeout_exhausted"
        assert draft.status == "failed"


def test_household_isolation_on_capture_and_job_endpoints(monkeypatch) -> None:
    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            _setup_household(client, household_name="Família A", username="admin-iso-a", password="senha-iso-segura-a")
            _create_account(client)
            response = client.post(
                "/api/captures/preview", files={"file": ("recibo.png", _png_bytes(), "image/png")}
            )
            capture_id = response.json()["id"]

            _setup_household(client, household_name="Família B", username="admin-iso-b", password="senha-iso-segura-b")
            assert client.get(f"/api/captures/{capture_id}").status_code == 404
            assert client.post(f"/api/captures/{capture_id}/retry").status_code == 404
            assert client.delete(f"/api/captures/{capture_id}").status_code == 404
            # The other household's capture list must not leak this row.
            other_list = client.get("/api/captures").json()
            assert capture_id not in {item["id"] for item in other_list}
    finally:
        app.dependency_overrides.clear()


def test_cancelling_a_queued_capture_prevents_the_worker_from_resurrecting_it(monkeypatch) -> None:
    """A cancel that lands while a job is in flight must stick -- the
    worker must never overwrite a cancelled draft's status with a
    proposal."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Cancelar", username="admin-cancel-1", password="senha-cancel-segura-1"
            )["household_id"]
            _create_account(client)
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = capture_worker.create_capture_job(
                    db,
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    max_attempts=3,
                )
                db.commit()
                draft_id, job_id = draft.id, job.id

            cancel_response = client.delete(f"/api/captures/{draft_id}")
            assert cancel_response.status_code == 200

            with _TestSessionLocal() as db:
                job = db.get(CaptureProcessingJob, job_id)
                assert job.status == "failed"
                assert job.error_code == "cancelled_by_user"

            # Even if something still tries to process it (a delayed
            # background task from before the cancel landed), it must bail
            # out honestly instead of overwriting the cancellation.
            asyncio.run(
                api_module._run_capture_job_background(job_id, None, session_factory=_TestSessionLocal)
            )
            with _TestSessionLocal() as db:
                draft = db.get(CaptureDraft, draft_id)
                assert draft.status == "cancelled"
    finally:
        app.dependency_overrides.clear()


def test_audit_events_never_carry_raw_extracted_text_or_payload(monkeypatch) -> None:
    _fake_ocr(monkeypatch, text="SEGREDO-NAO-DEVE-VAZAR-1234567890")
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Auditoria", username="admin-audit-1", password="senha-audit-segura-1"
            )["household_id"]
            _create_account(client)
            response = client.post(
                "/api/captures/preview", files={"file": ("recibo.png", _png_bytes(), "image/png")}
            )
            capture_id = response.json()["id"]

        with _TestSessionLocal() as db:
            from app.models import AuditEvent

            events = db.scalars(
                select(AuditEvent).where(AuditEvent.household_id == household_id)
            ).all()
            assert events, "expected capture.queued/capture.preview audit events"
            for event in events:
                assert "SEGREDO-NAO-DEVE-VAZAR" not in (event.details or "")
                assert "SEGREDO-NAO-DEVE-VAZAR" not in (event.before_state or "")
                assert "SEGREDO-NAO-DEVE-VAZAR" not in (event.after_state or "")

        # The original file on disk must be encrypted, not the raw bytes.
        with _TestSessionLocal() as db:
            draft = db.get(CaptureDraft, capture_id)
            document = db.get(Document, draft.document_id)
            from pathlib import Path

            on_disk = Path(document.encrypted_path).read_bytes()
            assert b"PNG" not in on_disk  # PNG magic header would be readable in plaintext
    finally:
        app.dependency_overrides.clear()


def test_duplicate_file_upload_is_rejected_before_any_second_job_is_queued(monkeypatch) -> None:
    """The pre-existing `Document(household_id, sha256)` uniqueness guard
    (unchanged by this slice) is what actually prevents duplicate
    submission from ever reaching the queue twice."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Duplicada", username="admin-dup-1", password="senha-dup-segura-1"
            )["household_id"]
            _create_account(client)
            payload = _png_bytes()
            first = client.post("/api/captures/preview", files={"file": ("recibo.png", payload, "image/png")})
            assert first.status_code == 201
            second = client.post("/api/captures/preview", files={"file": ("recibo.png", payload, "image/png")})
            assert second.status_code == 409

        with _TestSessionLocal() as db:
            assert len(db.scalars(select(CaptureProcessingJob).where(CaptureProcessingJob.household_id == household_id)).all()) == 1
            assert len(db.scalars(select(Document).where(Document.household_id == household_id)).all()) == 1
    finally:
        app.dependency_overrides.clear()


def test_concurrent_reclaim_of_a_stale_job_is_race_safe(monkeypatch) -> None:
    """Review item 1: two reclaimers racing on the same *stale processing*
    job (not a fresh `queued` one) must not both win. `status` alone does
    not change across a reclaim (`processing` -> `processing`), so a CAS
    predicate on `status` only would let a second reclaimer's `UPDATE ...
    WHERE status = 'processing'` still match after the first reclaimer's
    commit -- the persisted value never actually changed. `claim_capture_job`
    must also pin `attempts` (which *does* change on every successful
    claim) to the pre-image each caller observed.

    A true concurrent race is simulated deterministically -- without real
    threads, and without relying on `Session`/connection-pool caching
    behavior that is an implementation detail, not a guarantee -- by
    forcing worker B's own read to return a pinned, pre-claim snapshot
    (`attempts=1`, the exact pre-image a truly concurrent reader would have
    observed if its own SELECT executed before worker A's commit landed),
    while worker B's actual CAS `UPDATE` still runs for real against the
    already-committed row. This isolates exactly the property under test:
    given that pre-image, does `claim_capture_job` build a `WHERE` clause
    that only matches if nothing else has changed since?
    """

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Reclaim", username="admin-reclaim-1", password="senha-reclaim-segura-1"
            )["household_id"]
            _create_account(client)
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="processing",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = CaptureProcessingJob(
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    status="processing",
                    attempts=1,
                    max_attempts=5,
                    started_at=datetime.now(UTC) - timedelta(seconds=900),
                )
                db.add(job)
                db.commit()
                job_id = job.id
                pre_claim_started_at = job.started_at

            with _TestSessionLocal() as db_a, _TestSessionLocal() as db_b:
                claimed_a = capture_worker.claim_capture_job(
                    db_a, job_id, worker_id="worker-a", stale_after_seconds=600
                )
                assert claimed_a is not None

                # Pin worker B's read to the pre-claim snapshot it would
                # have observed had its own SELECT run before A's commit.
                stale_pre_image = SimpleNamespace(
                    status="processing", attempts=1, started_at=pre_claim_started_at
                )
                monkeypatch.setattr(db_b, "get", lambda *a, **k: stale_pre_image)

                claimed_b = capture_worker.claim_capture_job(
                    db_b, job_id, worker_id="worker-b", stale_after_seconds=600
                )
                assert claimed_b is None  # must lose the reclaim race

            with _TestSessionLocal() as db:
                refreshed = db.get(CaptureProcessingJob, job_id)
                assert refreshed.status == "processing"
                assert refreshed.attempts == 2  # incremented exactly once
                assert refreshed.claimed_by == "worker-a"
    finally:
        app.dependency_overrides.clear()


def test_late_cancellation_during_processing_is_not_resurrected_by_a_slow_worker(monkeypatch) -> None:
    """Review item 3: a cancellation that lands *while* the worker is still
    doing the heavy OCR/transcription work -- after the worker's own
    early "already cancelled" check has already passed -- must still win.
    The existing `test_cancelling_a_queued_capture_prevents_the_worker_from_resurrecting_it`
    only covers a cancel that lands *before* the job is ever claimed; this
    covers the real in-flight interleaving the PR itself flagged as an
    open risk: the worker's eventual `completed` write must not resurrect
    the draft, and must not silently replace `error_code="cancelled_by_user"`
    with a processing outcome."""

    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client,
                household_name="Família Corrida Cancelar",
                username="admin-race-cancel-1",
                password="senha-race-cancel-segura-1",
            )["household_id"]
            _create_account(client)

            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = capture_worker.create_capture_job(
                    db,
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    max_attempts=3,
                )
                db.commit()
                draft_id, job_id = draft.id, job.id

            def _ocr_that_races_a_cancellation(filename, payload, content_type):
                # Stands in for the household cancelling the draft while
                # this (slow, already-in-flight) OCR call is still running
                # -- the exact window review item 3 flagged. Calls the real
                # `cancel_capture` route function directly (its own,
                # independent DB session, exactly like a genuinely
                # concurrent request would use) rather than re-implementing
                # its mutations, but bypasses the TestClient/ASGI layer to
                # avoid nesting a second live request inside the threadpool
                # thread `run_in_threadpool` already runs this OCR call on.
                with _TestSessionLocal() as cancel_db:
                    household_user = cancel_db.scalar(
                        select(User).where(User.household_id == household_id)
                    )
                    api_module.cancel_capture(draft_id, user=household_user, db=cancel_db)
                return "Gastei R$ 45,00 no mercado hoje", "ocr_local"

            monkeypatch.setattr(
                smart_capture_module, "extract_document_text", _ocr_that_races_a_cancellation
            )

            with _TestSessionLocal() as db_worker:
                claimed = capture_worker.claim_capture_job(
                    db_worker, job_id, worker_id="worker-race-cancel", stale_after_seconds=600
                )
                assert claimed is not None
                asyncio.run(
                    api_module._process_claimed_capture_job(db_worker, claimed, payload=_png_bytes())
                )

            with _TestSessionLocal() as db:
                draft = db.get(CaptureDraft, draft_id)
                job = db.get(CaptureProcessingJob, job_id)
                assert draft.status == "cancelled"  # not resurrected into "preview"
                assert draft.proposal_json == "[]"  # no proposal written after all
                assert job.status == "failed"
                assert job.error_code == "cancelled_by_user"  # not overwritten to "completed"
    finally:
        app.dependency_overrides.clear()


def test_worker_fails_closed_when_job_household_disagrees_with_its_draft(monkeypatch) -> None:
    """Review item 2: `capture_processing_jobs.household_id` and its
    `capture_draft_id`'s own `household_id` are two independent FKs with no
    constraint tying them together. If that ever drifted apart (corrupted/
    inconsistent state), the worker must fail closed instead of silently
    processing -- and must never write to the foreign draft it does not
    actually own."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_a = _setup_household(
                client, household_name="Família Iso A", username="admin-iso-job-a", password="senha-iso-job-segura-a"
            )["household_id"]
            household_b = _setup_household(
                client, household_name="Família Iso B", username="admin-iso-job-b", password="senha-iso-job-segura-b"
            )["household_id"]

            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_a,
                    source_type="text",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                # Deliberately corrupted: job claims household B while the
                # draft it correlates to actually belongs to household A.
                job = CaptureProcessingJob(
                    household_id=household_b,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    status="queued",
                )
                db.add(job)
                db.commit()
                draft_id, job_id = draft.id, job.id

            with _TestSessionLocal() as db:
                claimed = capture_worker.claim_capture_job(
                    db, job_id, worker_id="worker-cross-household", stale_after_seconds=600
                )
                assert claimed is not None
                asyncio.run(
                    api_module._process_claimed_capture_job(db, claimed, payload=None)
                )

            with _TestSessionLocal() as db:
                job = db.get(CaptureProcessingJob, job_id)
                draft = db.get(CaptureDraft, draft_id)
                assert job.status == "failed"
                assert job.error_code == "cross_household_state"
                # The foreign draft (household A's) must be untouched.
                assert draft.status == "queued"
                assert draft.proposal_json == "[]"
    finally:
        app.dependency_overrides.clear()


def test_worker_fails_closed_when_document_belongs_to_another_household(monkeypatch) -> None:
    """Review item 2: same invariant as above, one hop further down --
    `capture_processing_jobs.document_id` may point at a `documents` row
    that does not belong to this job's own household. The worker must fail
    closed without reading/decrypting that document, while still being
    free to mark its *own* household's draft as failed."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_b = _setup_household(
                client, household_name="Família Iso Doc B", username="admin-iso-doc-b", password="senha-iso-doc-segura-b"
            )["household_id"]
            with _TestSessionLocal() as db:
                foreign_document = Document(
                    household_id=household_b,
                    original_name="recibo-b.png",
                    document_type="smart_capture",
                    sha256="b" * 64,
                    encrypted_path="ignored-for-this-test",
                    status="capture_processing",
                )
                db.add(foreign_document)
                db.commit()
                foreign_document_id = foreign_document.id

            household_a = _setup_household(
                client, household_name="Família Iso Doc A", username="admin-iso-doc-a", password="senha-iso-doc-segura-a"
            )["household_id"]
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_a,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = CaptureProcessingJob(
                    household_id=household_a,
                    capture_draft_id=draft.id,
                    document_id=foreign_document_id,  # belongs to household B
                    job_type="ocr",
                    content_type="image/png",
                    status="queued",
                )
                db.add(job)
                db.commit()
                draft_id, job_id = draft.id, job.id

            with _TestSessionLocal() as db:
                claimed = capture_worker.claim_capture_job(
                    db, job_id, worker_id="worker-cross-household-doc", stale_after_seconds=600
                )
                assert claimed is not None
                asyncio.run(
                    api_module._process_claimed_capture_job(db, claimed, payload=None)
                )

            with _TestSessionLocal() as db:
                job = db.get(CaptureProcessingJob, job_id)
                draft = db.get(CaptureDraft, draft_id)
                document = db.get(Document, foreign_document_id)
                assert job.status == "failed"
                assert job.error_code == "cross_household_state"
                assert draft.status == "failed"  # our own draft: allowed to mark failed
                assert document.status == "capture_processing"  # foreign document: untouched
    finally:
        app.dependency_overrides.clear()


def test_worker_fails_closed_when_account_or_user_belongs_to_another_household(monkeypatch) -> None:
    """Review item 2, the remaining two correlations: `documents.account_id`
    and `capture_drafts.user_id` may also point outside this job's own
    household. Covers both in one pass (account first, then user, on two
    independent jobs) since both take the same fail-closed path."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_b = _setup_household(
                client, household_name="Família Iso Acc B", username="admin-iso-acc-b", password="senha-iso-acc-segura-b"
            )["household_id"]
            foreign_account = _create_account(client)
            with _TestSessionLocal() as db:
                foreign_user = db.scalar(select(User).where(User.household_id == household_b))
                foreign_user_id = foreign_user.id

            household_a = _setup_household(
                client, household_name="Família Iso Acc A", username="admin-iso-acc-a", password="senha-iso-acc-segura-a"
            )["household_id"]
            _create_account(client)

            # -- Case 1: document.account_id points at household B's account. --
            with _TestSessionLocal() as db:
                document = Document(
                    household_id=household_a,
                    account_id=foreign_account["id"],
                    original_name="recibo-acc.png",
                    document_type="smart_capture",
                    sha256="c" * 64,
                    encrypted_path="ignored-for-this-test",
                    status="capture_processing",
                )
                db.add(document)
                db.flush()
                draft = CaptureDraft(
                    household_id=household_a,
                    document_id=document.id,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                job = CaptureProcessingJob(
                    household_id=household_a,
                    capture_draft_id=draft.id,
                    document_id=document.id,
                    job_type="ocr",
                    content_type="image/png",
                    status="queued",
                )
                db.add(job)
                db.commit()
                document_id, job_id = document.id, job.id

            with _TestSessionLocal() as db:
                claimed = capture_worker.claim_capture_job(
                    db, job_id, worker_id="worker-cross-household-acc", stale_after_seconds=600
                )
                assert claimed is not None
                asyncio.run(api_module._process_claimed_capture_job(db, claimed, payload=None))

            with _TestSessionLocal() as db:
                job = db.get(CaptureProcessingJob, job_id)
                document = db.get(Document, document_id)
                account = db.get(Account, foreign_account["id"])
                assert job.status == "failed"
                assert job.error_code == "cross_household_state"
                assert document.status == "capture_failed"  # our own document: allowed
                assert account.household_id == household_b  # foreign account untouched/unread

            # -- Case 2: capture_draft.user_id points at household B's user. --
            with _TestSessionLocal() as db:
                draft2 = CaptureDraft(
                    household_id=household_a,
                    user_id=foreign_user_id,
                    source_type="text",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft2)
                db.flush()
                job2 = CaptureProcessingJob(
                    household_id=household_a,
                    capture_draft_id=draft2.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    status="queued",
                )
                db.add(job2)
                db.commit()
                draft2_id, job2_id = draft2.id, job2.id

            with _TestSessionLocal() as db:
                claimed2 = capture_worker.claim_capture_job(
                    db, job2_id, worker_id="worker-cross-household-user", stale_after_seconds=600
                )
                assert claimed2 is not None
                asyncio.run(api_module._process_claimed_capture_job(db, claimed2, payload=None))

            with _TestSessionLocal() as db:
                job2 = db.get(CaptureProcessingJob, job2_id)
                draft2 = db.get(CaptureDraft, draft2_id)
                assert job2.status == "failed"
                assert job2.error_code == "cross_household_state"
                assert draft2.status == "failed"
    finally:
        app.dependency_overrides.clear()


def test_retry_is_refused_for_a_cancelled_capture(monkeypatch) -> None:
    """`cancel_capture` marks an in-flight job `"failed"` with
    `error_code="cancelled_by_user"` -- its own honest terminal state --
    which means `job.status == "failed"` alone cannot tell `/retry` apart
    from a genuinely failed job. Without checking the draft's own status,
    `/retry` would be a second, user-triggered path to resurrect a
    cancelled capture (besides the worker race the atomic finalize above
    already closes)."""

    _fake_ocr(monkeypatch)
    client = _client()
    try:
        with client:
            household_id = _setup_household(
                client, household_name="Família Retry Cancelado", username="admin-retry-cancel-1", password="senha-retry-cancel-segura-1"
            )["household_id"]
            _create_account(client)
            # Manually enqueue a job still `queued` (never claimed) --
            # mirrors `test_cancelling_a_queued_capture_prevents_the_worker_from_resurrecting_it` --
            # so cancelling it lands `job.status == "failed"` for the same
            # reason a genuinely failed job would: without checking the
            # draft's own status, `/retry`'s `job.status != "failed"` guard
            # alone cannot tell the two apart.
            with _TestSessionLocal() as db:
                draft = CaptureDraft(
                    household_id=household_id,
                    source_type="image",
                    detected_type="auto",
                    status="queued",
                    processor="pending",
                    proposal_json="[]",
                )
                db.add(draft)
                db.flush()
                capture_worker.create_capture_job(
                    db,
                    household_id=household_id,
                    capture_draft_id=draft.id,
                    document_id=None,
                    job_type="ocr",
                    content_type="image/png",
                    max_attempts=3,
                )
                db.commit()
                capture_id = draft.id

            cancel_response = client.delete(f"/api/captures/{capture_id}")
            assert cancel_response.status_code == 200

            with _TestSessionLocal() as db:
                job = db.get(CaptureDraft, capture_id).processing_job
                assert job.status == "failed"
                assert job.error_code == "cancelled_by_user"

            retry_response = client.post(f"/api/captures/{capture_id}/retry")
            assert retry_response.status_code == 409
            assert "cancelad" in retry_response.json()["detail"].casefold()

            with _TestSessionLocal() as db:
                draft = db.get(CaptureDraft, capture_id)
                job = db.get(CaptureProcessingJob, job.id)
                assert draft.status == "cancelled"  # unchanged -- retry never revived it
                assert job.status == "failed"  # unchanged -- never moved back to "queued"
    finally:
        app.dependency_overrides.clear()
