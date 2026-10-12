"""Background worker: claims evaluation jobs and runs the pipeline with
checkpoints, heartbeats and classified failure handling.

Run:  python -m app.worker            (loops forever)
      python -m app.worker --once     (process at most one job; used in demos/tests)
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import tarfile
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import update

from app import db
from app.config import settings
from app.evaluation import engine
from app.evaluation.prompts import PROMPT_VERSION
from app.evaluation.retrieval import BM25Index
from app.ingest.classify import build_inventory
from app.ingest.safe_zip import UnsafeArchive, safe_extract
from app.ingest.sources import InvalidSource, parse_github_url, validate_live_url
from app.jobqueue import LeaseLost, claim_next, complete, fail, heartbeat, reap_expired_leases
from app.llm.client import LLMClient
from app.llm.fake import FakeProvider
from app.llm.provider import GeminiProvider, LLMError
from app.models import (
    ArtifactKind,
    ArtifactStatus,
    Assignment,
    AuditLog,
    CriterionScore,
    EvaluationJob,
    EvaluationResult,
    HumanReview,
    ResultStatus,
    Rubric,
    StageCheckpoint,
    Submission,
    SubmissionArtifact,
    SubmissionStatus,
)
from app.storage import LocalStorage

log = logging.getLogger("worker")


class PermanentFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def make_provider():
    if settings.llm_provider == "gemini":
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings.llm_timeout_s)
    return FakeProvider()


# --------------------------------------------------------------------- ingestion


def _download_github(url: str, dest: Path) -> Path:
    repo = parse_github_url(url)
    tgz = dest / "repo.tar.gz"
    headers = {"Accept": "application/vnd.github+json"}
    if tok := os.getenv("GITHUB_TOKEN"):  # GitHub App installation token in production
        headers["Authorization"] = f"Bearer {tok}"
    n = 0
    try:
        with httpx.stream("GET", repo.archive_api_url, headers=headers, follow_redirects=True, timeout=60) as r:
            if r.status_code in (403, 404):
                raise PermanentFailure("github_repo_inaccessible", f"{repo.owner}/{repo.repo} is private or does not exist")
            r.raise_for_status()
            with open(tgz, "wb") as out:
                for chunk in r.iter_bytes(1024 * 1024):
                    n += len(chunk)
                    if n > settings.max_upload_bytes:
                        raise PermanentFailure("repo_too_large", "repository archive exceeds the size limit")
                    out.write(chunk)
    except httpx.TransportError as e:
        raise ConnectionError(f"github download failed: {e}") from e  # retryable
    out_dir = dest / "repo"
    with tarfile.open(tgz) as tf:
        members = tf.getmembers()
        if len(members) > settings.max_files:
            raise PermanentFailure("too_many_files", f"{len(members)} files > limit")
        if sum(m.size for m in members) > settings.max_extracted_bytes:
            raise PermanentFailure("repo_too_large", "repository is too large to analyse")
        tf.extractall(out_dir, filter="data")  # PEP 706: blocks traversal, links outside, devices
    tgz.unlink()
    return out_dir


def _pdf_to_text(src: Path, out_dir: Path) -> None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(src))
        pages = [p.extract_text() or "" for p in reader.pages[:200]]
    except Exception as e:  # corrupt PDF: skip, don't fail the whole evaluation
        log.warning("pdf extraction failed for %s: %s", src.name, e)
        return
    (out_dir / f"{src.stem}.pdf.md").write_text("\n\n".join(pages), encoding="utf-8")


def ingest(storage: LocalStorage, job_id: str, artifacts: list[SubmissionArtifact]) -> tuple[Path, list[str]]:
    """Materialise every artifact into one working tree. Deterministic and
    re-runnable, so it is simply redone on retry (no checkpoint needed)."""
    root = storage.workdir(job_id) / "project"
    if root.exists():
        import shutil

        shutil.rmtree(root)
    root.mkdir(parents=True)
    notes: list[str] = []
    for a in artifacts:
        if a.status != ArtifactStatus.stored and a.kind in (ArtifactKind.zip, ArtifactKind.pdf, ArtifactKind.document):
            notes.append(f"{a.kind.value} artifact {a.id} was never uploaded; skipped")
            continue
        sub = root / f"{a.kind.value}_{a.id[:8]}"
        sub.mkdir()
        if a.kind == ArtifactKind.zip:
            safe_extract(storage.path(a.storage_key), sub, max_total_bytes=settings.max_extracted_bytes,
                         max_files=settings.max_files, max_ratio=settings.max_compression_ratio)
        elif a.kind == ArtifactKind.github:
            _download_github(a.source_url, sub)
        elif a.kind in (ArtifactKind.pdf, ArtifactKind.document):
            src = storage.path(a.storage_key)
            if src.suffix.lower() == ".pdf":
                _pdf_to_text(src, sub)
            else:
                (sub / (a.filename or "document.txt")).write_bytes(src.read_bytes())
        elif a.kind == ArtifactKind.live_url:
            validate_live_url(a.source_url)
            notes.append(f"live app {a.source_url} recorded; the browser-probe stage is not in the PoC")
        else:  # video, drive
            notes.append(f"{a.kind.value} artifact not processed by the PoC (see DESIGN.md §5)")
    return root, notes


# --------------------------------------------------------------------- pipeline


def _checkpoint(s, job_id: str, stage: str) -> dict | None:
    cp = s.get(StageCheckpoint, (job_id, stage))
    return cp.payload if cp else None


def _save_checkpoint(job_id: str, stage: str, payload: dict) -> None:
    with db.session_scope() as s:
        s.merge(StageCheckpoint(job_id=job_id, stage=stage, payload=payload))


def run_job(job_id: str, worker_id: str, provider=None, storage: LocalStorage | None = None) -> None:
    storage = storage or LocalStorage()
    with db.session_scope() as s:
        job = s.get(EvaluationJob, job_id)
        sub = s.get(Submission, job.submission_id)
        assignment = s.get(Assignment, sub.assignment_id)
        rubric = s.get(Rubric, job.rubric_id)
        criteria = [
            {"id": c.id, "key": c.key, "title": c.title, "description": c.description, "weight": c.weight,
             "max_score": c.max_score, "scoring_guide": c.scoring_guide, "evidence_sources": c.evidence_sources}
            for c in rubric.criteria
        ]
        artifacts = list(sub.artifacts)
        brief = f"{assignment.title}\n{assignment.description}"
        s.execute(update(Submission).where(Submission.id == sub.id).values(status=SubmissionStatus.evaluating))

    def beat(stage: str | None = None, progress: int | None = None) -> None:
        with db.session_scope() as hs:
            heartbeat(hs, job_id, worker_id, settings.lease_seconds, stage=stage, progress=progress,
                      tokens_used=llm.usage.total)

    llm = LLMClient(provider or make_provider(), max_retries=settings.llm_max_retries,
                    token_budget=settings.job_token_budget, on_call=lambda: beat())

    beat("ingest", 5)
    root, notes = ingest(storage, job_id, artifacts)

    beat("inventory", 15)
    inv = build_inventory(root, settings.max_file_bytes_for_analysis)
    chunks = engine.build_chunks(root, inv, settings.max_file_bytes_for_analysis)
    if not chunks:
        raise PermanentFailure("empty_submission", "no analysable source or documentation files found")
    injection_refs = engine.scan_prompt_injection(chunks)
    facts = {**inv.facts, "ingestion_notes": notes}

    with db.session_scope() as s:
        ctx = _checkpoint(s, job_id, "understand")
    if ctx is None:
        beat("understand", 25)
        ctx = engine.understand(llm, inv, chunks, brief)
        _save_checkpoint(job_id, "understand", ctx)

    index = BM25Index(chunks)
    scores = []
    for i, crit in enumerate(criteria):
        stage = f"criterion:{crit['key']}"
        with db.session_scope() as s:
            done = _checkpoint(s, job_id, stage)
        if done is None:
            beat(stage, 40 + int(50 * i / max(1, len(criteria))))
            done = engine.evaluate_criterion(llm, crit, ctx, index, facts)
            _save_checkpoint(job_id, stage, done)
        scores.append(done)

    beat("aggregate", 95)
    agg = engine.aggregate(scores, injection_refs=injection_refs, facts=facts,
                           threshold=settings.review_confidence_threshold)
    flags = sorted({f for sc in scores for f in sc["flags"]} | set(agg.reasons))
    usage = {"tokens_in": llm.usage.tokens_in, "tokens_out": llm.usage.tokens_out, "calls": llm.usage.calls}

    # Persist result + finish job atomically. complete() re-checks the lease, so a
    # zombie worker that lost its lease cannot overwrite the new owner's work.
    with db.session_scope() as s:
        job = s.get(EvaluationJob, job_id)
        s.execute(update(EvaluationResult).where(EvaluationResult.submission_id == job.submission_id,
                                                 EvaluationResult.is_current.is_(True)).values(is_current=False))
        status = ResultStatus.pending_review if agg.needs_review else ResultStatus.approved
        result = EvaluationResult(
            job_id=job.id, submission_id=job.submission_id, rubric_id=job.rubric_id, status=status,
            total_score=agg.total, overall_confidence=agg.confidence,
            summary=ctx["project"]["purpose"] + " " + ctx["project"]["architecture"],
            flags=flags, model=llm.model_name, prompt_version=PROMPT_VERSION,
        )
        s.add(result)
        s.flush()
        for sc in scores:
            s.add(CriterionScore(
                result_id=result.id, criterion_id=sc["criterion_id"], criterion_key=sc["key"], score=sc["score"],
                max_score=sc["max_score"], confidence=sc["confidence"], rationale=sc["rationale"],
                strengths=sc["strengths"], improvements=sc["improvements"], evidence=sc["evidence"],
            ))
        if agg.needs_review:
            s.add(HumanReview(result_id=result.id, requested_by=job.requested_by, source="auto",
                              reason="auto: " + ", ".join(agg.reasons)))
        s.execute(update(Submission).where(Submission.id == job.submission_id).values(
            status=SubmissionStatus.evaluated if agg.needs_review else SubmissionStatus.finalized))
        s.add(AuditLog(action="evaluation.completed", entity="evaluation_job", entity_id=job.id,
                       data={"result_id": result.id, "total": agg.total, "review": agg.needs_review, **usage}))
        complete(s, job, worker_id, usage)
    storage.cleanup_workdir(job_id)
    log.info("job %s done: %.1f/100 review=%s calls=%d tokens=%d", job_id, agg.total, agg.needs_review,
             usage["calls"], llm.usage.total)
    # Notification (email / websocket push) is emitted from the audit/outbox row by a separate publisher.


def process_one(worker_id: str, provider=None, storage: LocalStorage | None = None) -> str | None:
    with db.session_scope() as s:
        reap_expired_leases(s)
        job = claim_next(s, worker_id, settings.lease_seconds)
        if not job:
            return None
        job_id = job.id
    try:
        run_job(job_id, worker_id, provider, storage)
    except LeaseLost:
        log.warning("lost lease on %s; abandoning without writing", job_id)
    except Exception as e:  # classify and record
        retryable, code = _classify(e)
        with db.session_scope() as s:
            job = s.get(EvaluationJob, job_id)
            try:
                new_status = fail(s, job, worker_id, code=code, detail=f"{type(e).__name__}: {e}", retryable=retryable,
                                  not_before_s=getattr(e, "retry_after", None))
            except LeaseLost:
                return job_id
            if new_status.value == "failed":
                s.execute(update(Submission).where(Submission.id == job.submission_id)
                          .values(status=SubmissionStatus.failed))
                s.add(AuditLog(action="evaluation.failed", entity="evaluation_job", entity_id=job_id,
                               data={"code": code}))
        log.error("job %s failed (%s, retryable=%s): %s", job_id, code, retryable, e)
    return job_id


def _classify(e: Exception) -> tuple[bool, str]:
    if isinstance(e, (UnsafeArchive, InvalidSource, PermanentFailure)):
        return False, e.code
    if isinstance(e, LLMError):
        return e.retryable, e.code
    if isinstance(e, (ConnectionError, TimeoutError, httpx.TransportError)):
        return True, "network_error"
    return True, "internal_error"  # unknown: retry, attempts cap bounds the damage


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    db.init_db()
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    log.info("worker %s started (provider=%s)", worker_id, settings.llm_provider)
    while True:
        did = process_one(worker_id)
        if args.once:
            break
        if not did:
            time.sleep(2)  # with SQS this is a long-poll ReceiveMessage instead


if __name__ == "__main__":
    main()
