"""Job state machine + leasing. The database row is the source of truth; the broker
(SQS in production, DB polling in this PoC) only delivers "job X is ready" hints.

Why this split: SQS is at-least-once, so a message can arrive twice or after the
job was cancelled. Workers therefore *claim* the job with a guarded UPDATE; only
one claim can succeed, which makes the consumer idempotent regardless of
duplicate deliveries.

    queued ──claim──▶ running ──ok──▶ succeeded
       ▲                 │  │
       │      retryable  │  └─permanent / attempts exhausted──▶ failed (DLQ + alert)
       └──run_after── retry_wait ◀─┘
    running + lease expired (worker crashed) ──reaper──▶ retry_wait / failed
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models import EvaluationAttempt, EvaluationJob, JobStatus, utcnow


class LeaseLost(Exception):
    """The worker no longer owns the job (lease expired and someone else took it).
    It must stop immediately and not write results - fencing against zombie workers."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def claim_next(s: Session, worker_id: str, lease_seconds: int) -> EvaluationJob | None:
    now = _now()
    runnable = (
        select(EvaluationJob.id)
        .where(
            EvaluationJob.status.in_([JobStatus.queued, JobStatus.retry_wait]),
            EvaluationJob.run_after <= now,
        )
        .order_by(EvaluationJob.priority, EvaluationJob.run_after)
        .limit(5)
    )
    if s.bind.dialect.name == "postgresql":
        runnable = runnable.with_for_update(skip_locked=True)  # workers don't block each other
    for job_id in s.scalars(runnable).all():
        # Guarded transition: succeeds for exactly one worker.
        res = s.execute(
            update(EvaluationJob)
            .where(
                EvaluationJob.id == job_id,
                EvaluationJob.status.in_([JobStatus.queued, JobStatus.retry_wait]),
            )
            .values(
                status=JobStatus.running,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=EvaluationJob.attempts + 1,
                updated_at=now,
            )
        )
        if res.rowcount == 1:
            job = s.get(EvaluationJob, job_id)
            s.refresh(job)
            s.add(EvaluationAttempt(job_id=job.id, attempt_no=job.attempts, worker_id=worker_id))
            s.commit()
            return job
    s.commit()
    return None


def heartbeat(s: Session, job_id: str, worker_id: str, lease_seconds: int, *, stage: str | None = None,
              progress: int | None = None, tokens_used: int | None = None) -> None:
    values: dict = {"lease_expires_at": _now() + timedelta(seconds=lease_seconds), "updated_at": _now()}
    if stage is not None:
        values["current_stage"] = stage
    if progress is not None:
        values["progress"] = progress
    if tokens_used is not None:
        values["tokens_used"] = tokens_used
    res = s.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job_id, EvaluationJob.lease_owner == worker_id,
               EvaluationJob.status == JobStatus.running)
        .values(**values)
    )
    s.commit()
    if res.rowcount != 1:
        raise LeaseLost(job_id)


def _finish_attempt(s: Session, job: EvaluationJob, status: str, code: str | None = None, detail: str | None = None,
                    usage: dict | None = None) -> None:
    att = s.scalars(
        select(EvaluationAttempt).where(EvaluationAttempt.job_id == job.id, EvaluationAttempt.attempt_no == job.attempts)
    ).first()
    if att:
        att.status, att.error_code, att.error_detail, att.finished_at = status, code, detail, utcnow()
        if usage:
            att.tokens_in, att.tokens_out, att.llm_calls = usage["tokens_in"], usage["tokens_out"], usage["calls"]


def complete(s: Session, job: EvaluationJob, worker_id: str, usage: dict | None = None) -> None:
    res = s.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id, EvaluationJob.lease_owner == worker_id, EvaluationJob.status == JobStatus.running)
        .values(status=JobStatus.succeeded, progress=100, finished_at=_now(), lease_owner=None, lease_expires_at=None)
    )
    if res.rowcount != 1:
        s.rollback()
        raise LeaseLost(job.id)
    _finish_attempt(s, job, "succeeded", usage=usage)


def backoff_seconds(attempt: int, base: int = 30, cap: int = 1800) -> float:
    return random.uniform(base, min(cap, base * 2**attempt))


def fail(s: Session, job: EvaluationJob, worker_id: str, *, code: str, detail: str, retryable: bool,
         usage: dict | None = None, not_before_s: float | None = None) -> JobStatus:
    """not_before_s: provider-mandated wait (quota reset). Such deferrals are not the
    job's fault, so they don't consume one of its attempts."""
    s.refresh(job)
    deferral = retryable and not_before_s is not None
    exhausted = job.attempts >= job.max_attempts and not deferral
    new_status = JobStatus.retry_wait if retryable and not exhausted else JobStatus.failed
    values = dict(status=new_status, last_error_code=code, last_error=detail[:2000], lease_owner=None,
                  lease_expires_at=None, updated_at=_now())
    if deferral:
        values["attempts"] = EvaluationJob.attempts - 1
    if new_status == JobStatus.retry_wait:
        wait = max(backoff_seconds(job.attempts), not_before_s or 0)
        values["run_after"] = _now() + timedelta(seconds=wait)
    else:
        values["finished_at"] = _now()
    res = s.execute(
        update(EvaluationJob)
        .where(EvaluationJob.id == job.id, EvaluationJob.lease_owner == worker_id)
        .values(**values)
    )
    if res.rowcount != 1:
        raise LeaseLost(job.id)
    _finish_attempt(s, job, "failed", code, detail[:2000], usage)
    return new_status


def reap_expired_leases(s: Session) -> int:
    """Run periodically (any worker, or a scheduled task). A running job whose lease
    expired means its worker died (OOM, spot interruption, deploy). Put it back."""
    now = _now()
    stale = s.scalars(
        select(EvaluationJob).where(EvaluationJob.status == JobStatus.running, EvaluationJob.lease_expires_at < now)
    ).all()
    n = 0
    for job in stale:
        exhausted = job.attempts >= job.max_attempts
        res = s.execute(
            update(EvaluationJob)
            .where(EvaluationJob.id == job.id, EvaluationJob.status == JobStatus.running,
                   or_(EvaluationJob.lease_expires_at < now, EvaluationJob.lease_expires_at.is_(None)))
            .values(
                status=JobStatus.failed if exhausted else JobStatus.retry_wait,
                run_after=now, lease_owner=None, lease_expires_at=None,
                last_error_code="worker_lost", last_error="lease expired; worker presumed dead",
                **({"finished_at": now} if exhausted else {}),
            )
        )
        if res.rowcount:
            _finish_attempt(s, job, "lost", "worker_lost", "lease expired")
            n += 1
    s.commit()
    return n
