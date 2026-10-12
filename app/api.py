"""REST API (FastAPI). Thin: validates, authorizes, writes state, enqueues work.
Nothing slow (downloads, extraction, LLM calls) ever runs in a request."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timezone

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db
from app.auth import (
    assert_can_read_submission,
    current_user,
    evaluation_limiter,
    is_course_staff,
    require_role,
    write_limiter,
)
from app.config import settings
from app.ingest.sources import InvalidSource, parse_drive_url, parse_github_url, validate_live_url
from app.models import (
    ACTIVE_JOB_STATUSES,
    ArtifactKind,
    ArtifactStatus,
    Assignment,
    AuditLog,
    CourseMembership,
    CriterionScore,
    EvaluationJob,
    EvaluationResult,
    HumanReview,
    IdempotencyRecord,
    ResultStatus,
    ReviewStatus,
    Role,
    Rubric,
    RubricCriterion,
    Submission,
    SubmissionArtifact,
    SubmissionStatus,
    User,
    utcnow,
)
from app.schemas import (
    ArtifactOut,
    EvaluationStart,
    JobOut,
    ResultOut,
    ReviewDecision,
    ReviewOut,
    ReviewRequest,
    RubricIn,
    RubricOut,
    SubmissionCreate,
    SubmissionOut,
)
from app.storage import LocalStorage

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # production: Alembic migrations run as a pre-deploy step instead
    yield


app = FastAPI(title="AlmaBetter Project Evaluation API", version="1.0.0", lifespan=lifespan)
storage = LocalStorage()

MAGIC = {ArtifactKind.zip: [b"PK\x03\x04"], ArtifactKind.pdf: [b"%PDF-"]}


@app.exception_handler(IntegrityError)
def _integrity(_: Request, exc: IntegrityError):
    return JSONResponse(status_code=409, content={"detail": "conflicting concurrent update, retry"})


# ------------------------------------------------------------------ helpers


def _hash_body(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _replay(s: Session, user: User, key: str | None, endpoint: str, body: dict) -> JSONResponse | None:
    """Idempotency-Key replay. Same key + same body -> original response.
    Same key + different body -> 422 (client bug; never silently reuse)."""
    if not key:
        return None
    rec = s.get(IdempotencyRecord, (user.id, key))
    if not rec:
        return None
    if rec.endpoint != endpoint or rec.request_hash != _hash_body(body):
        raise HTTPException(422, "Idempotency-Key reused with a different request")
    return JSONResponse(status_code=rec.response_status, content=rec.response_body,
                        headers={"Idempotent-Replayed": "true"})


def _remember(s: Session, user: User, key: str | None, endpoint: str, body: dict, code: int, resp: dict) -> None:
    if key:
        s.add(IdempotencyRecord(user_id=user.id, key=key, endpoint=endpoint, request_hash=_hash_body(body),
                                response_status=code, response_body=json.loads(json.dumps(resp, default=str))))


def _sub_out(s: Session, sub: Submission) -> dict:
    latest = s.scalar(select(EvaluationJob.id).where(EvaluationJob.submission_id == sub.id)
                      .order_by(EvaluationJob.created_at.desc()).limit(1))
    arts = [
        ArtifactOut(
            id=a.id, kind=a.kind, status=a.status.value, source_url=a.source_url, filename=a.filename,
            size_bytes=a.size_bytes, sha256=a.sha256,
            upload_url=(f"/v1/submissions/{sub.id}/artifacts/{a.id}/content"
                        if a.status == ArtifactStatus.pending_upload else None),
        )
        for a in sub.artifacts
    ]
    return SubmissionOut(id=sub.id, assignment_id=sub.assignment_id, student_id=sub.student_id,
                         attempt_no=sub.attempt_no, status=sub.status.value, created_at=sub.created_at,
                         artifacts=arts, latest_job_id=latest).model_dump(mode="json")


def _job_out(j: EvaluationJob) -> dict:
    return JobOut(job_id=j.id, submission_id=j.submission_id, rubric_id=j.rubric_id, status=j.status.value,
                  current_stage=j.current_stage, progress=j.progress, attempts=j.attempts,
                  max_attempts=j.max_attempts, last_error_code=j.last_error_code, created_at=j.created_at,
                  finished_at=j.finished_at).model_dump(mode="json")


def _get_submission(s: Session, user: User, sub_id: str) -> Submission:
    sub = s.get(Submission, sub_id)
    if not sub:
        raise HTTPException(404, "submission not found")
    assert_can_read_submission(s, user, sub)
    return sub


def _audit(s: Session, actor: User, action: str, entity: str, entity_id: str, **data) -> None:
    s.add(AuditLog(actor_id=actor.id, action=action, entity=entity, entity_id=entity_id, data=data))


# ------------------------------------------------------------------ health


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(s: Session = Depends(db.get_session)):
    s.execute(text("SELECT 1"))
    return {"status": "ready"}


# ------------------------------------------------------------------ submissions


@app.post("/v1/submissions", status_code=201)
def create_submission(
    body: SubmissionCreate,
    idempotency_key: str | None = Header(default=None, max_length=128),
    user: User = Depends(require_role(Role.student)),
    s: Session = Depends(db.get_session),
):
    write_limiter.check(user.id)
    payload = body.model_dump(mode="json")
    if replay := _replay(s, user, idempotency_key, "create_submission", payload):
        return replay
    asg = s.get(Assignment, body.assignment_id)
    if not asg or not s.get(CourseMembership, (asg.course_id, user.id)):
        raise HTTPException(404, "assignment not found")
    due = asg.due_at.replace(tzinfo=asg.due_at.tzinfo or timezone.utc) if asg.due_at else None
    if due and utcnow() > due:
        raise HTTPException(422, "assignment deadline has passed")
    for a in body.artifacts:
        if a.kind.value not in asg.allowed_artifact_kinds:
            raise HTTPException(422, f"{a.kind.value} artifacts are not accepted for this assignment")
        if a.size_bytes and a.size_bytes > settings.max_upload_bytes:
            raise HTTPException(413, f"{a.filename} exceeds {settings.max_upload_bytes} bytes")
        try:
            if a.kind == ArtifactKind.github:
                parse_github_url(a.source_url)
            elif a.kind == ArtifactKind.drive:
                parse_drive_url(a.source_url)
            elif a.kind == ArtifactKind.live_url:
                validate_live_url(a.source_url, resolve=False)  # full DNS check again in the worker
        except InvalidSource as e:
            raise HTTPException(422, {"code": e.code, "message": str(e)}) from e

    attempt = (s.scalar(select(func.max(Submission.attempt_no)).where(
        Submission.assignment_id == asg.id, Submission.student_id == user.id)) or 0) + 1
    sub = Submission(assignment_id=asg.id, student_id=user.id, attempt_no=attempt,
                     idempotency_key=idempotency_key, notes=body.notes)
    for a in body.artifacts:
        linked = a.kind in (ArtifactKind.github, ArtifactKind.drive, ArtifactKind.live_url)
        sub.artifacts.append(SubmissionArtifact(
            kind=a.kind, source_url=a.source_url, filename=a.filename, size_bytes=a.size_bytes,
            status=ArtifactStatus.stored if linked else ArtifactStatus.pending_upload))
    s.add(sub)
    s.flush()
    resp = _sub_out(s, sub)
    _remember(s, user, idempotency_key, "create_submission", payload, 201, resp)
    _audit(s, user, "submission.created", "submission", sub.id)
    s.commit()
    return resp


@app.get("/v1/submissions/{submission_id}")
def get_submission(submission_id: str, user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    return _sub_out(s, _get_submission(s, user, submission_id))


@app.post("/v1/submissions/{submission_id}/artifacts/{artifact_id}/content")
def upload_artifact(
    submission_id: str,
    artifact_id: str,
    file: UploadFile = File(...),
    user: User = Depends(require_role(Role.student)),
    s: Session = Depends(db.get_session),
):
    """PoC upload path. Production: the create call returns a presigned S3 PUT
    (multipart for >100 MB) and S3 -> EventBridge triggers validation; the API
    never proxies file bytes."""
    write_limiter.check(user.id)
    sub = _get_submission(s, user, submission_id)
    if sub.student_id != user.id:
        raise HTTPException(404, "submission not found")
    if sub.status != SubmissionStatus.draft:
        raise HTTPException(409, "submission is frozen; create a new attempt to change files")
    art = s.get(SubmissionArtifact, artifact_id)
    if not art or art.submission_id != sub.id:
        raise HTTPException(404, "artifact not found")
    if art.status != ArtifactStatus.pending_upload:
        raise HTTPException(409, "artifact already uploaded")
    head = file.file.read(8)
    file.file.seek(0)
    if art.kind in MAGIC and not any(head.startswith(m) for m in MAGIC[art.kind]):
        art.status, art.rejection_reason = ArtifactStatus.rejected, "content does not match declared type"
        s.commit()
        raise HTTPException(415, "file content does not match declared type")
    key = f"submissions/{sub.id}/{art.id}/{art.filename}"
    try:
        size, sha = storage.put_stream(key, file.file, settings.max_upload_bytes)
    except ValueError as e:
        raise HTTPException(413, str(e)) from e
    # Guarded transition protects against a concurrent duplicate upload.
    res = s.execute(update(SubmissionArtifact)
                    .where(SubmissionArtifact.id == art.id, SubmissionArtifact.status == ArtifactStatus.pending_upload)
                    .values(status=ArtifactStatus.stored, storage_key=key, size_bytes=size, sha256=sha))
    if res.rowcount != 1:
        raise HTTPException(409, "artifact already uploaded")
    s.commit()
    s.refresh(sub)
    return _sub_out(s, sub)


# ------------------------------------------------------------------ evaluations


@app.post("/v1/submissions/{submission_id}/evaluations")
def start_evaluation(
    submission_id: str,
    response: Response,
    body: EvaluationStart | None = None,
    idempotency_key: str | None = Header(default=None, max_length=128),
    user: User = Depends(current_user),
    s: Session = Depends(db.get_session),
):
    """Idempotent. Calling it five times yields one job:
      1. a job with the same idempotency key is returned as-is;
      2. otherwise an *active* job for (submission, active rubric) is returned;
      3. otherwise a new job is inserted - and the partial unique index makes two
         concurrent inserts collapse into one (the loser re-reads the winner)."""
    body = body or EvaluationStart()
    sub = _get_submission(s, user, submission_id)
    staff = sub.student_id != user.id
    if body.force and not staff:
        raise HTTPException(403, "only staff can force a re-evaluation")
    rubric = s.scalar(select(Rubric).where(Rubric.assignment_id == sub.assignment_id, Rubric.is_active.is_(True)))
    if not rubric:
        raise HTTPException(409, "assignment has no active rubric")
    usable = [a for a in sub.artifacts if a.status == ArtifactStatus.stored]
    if not usable:
        raise HTTPException(409, "upload at least one artifact before requesting evaluation")

    if body.force:
        job_key = f"{sub.id}:{rubric.id}:force:{idempotency_key or uuid.uuid4().hex}"
    else:
        # Without a client key, a student gets exactly one evaluation per rubric version.
        job_key = f"{sub.id}:{rubric.id}:{idempotency_key or 'auto'}"

    def existing() -> EvaluationJob | None:
        j = s.scalar(select(EvaluationJob).where(EvaluationJob.idempotency_key == job_key))
        if j or body.force:
            return j
        return s.scalar(select(EvaluationJob).where(
            EvaluationJob.submission_id == sub.id, EvaluationJob.rubric_id == rubric.id,
            EvaluationJob.status.in_(ACTIVE_JOB_STATUSES)))

    if job := existing():
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
        return _job_out(job)

    evaluation_limiter.check(user.id)
    if sub.status == SubmissionStatus.draft:
        sub.status, sub.submitted_at = SubmissionStatus.submitted, utcnow()  # freeze artifacts
    job = EvaluationJob(submission_id=sub.id, rubric_id=rubric.id, requested_by=user.id,
                        idempotency_key=job_key, max_attempts=settings.job_max_attempts,
                        priority=50 if staff else 100)
    s.add(job)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()  # lost the race: someone else created the job a moment ago
        job = existing()
        if not job:
            raise
        response.status_code = status.HTTP_200_OK
        return _job_out(job)
    _audit(s, user, "evaluation.requested", "evaluation_job", job.id, force=body.force, reason=body.reason)
    s.commit()
    # Production: after commit, publish {"job_id": ...} to SQS. If that publish fails,
    # the reconciler re-publishes jobs left in 'queued' for > 2 minutes.
    response.status_code = status.HTTP_202_ACCEPTED
    response.headers["Location"] = f"/v1/evaluations/{job.id}"
    return _job_out(job)


def _get_job(s: Session, user: User, job_id: str) -> EvaluationJob:
    job = s.get(EvaluationJob, job_id)
    if not job:
        raise HTTPException(404, "evaluation not found")
    _get_submission(s, user, job.submission_id)
    return job


@app.get("/v1/evaluations/{job_id}")
def get_evaluation_status(job_id: str, user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    return _job_out(_get_job(s, user, job_id))


def _result_out(s: Session, r: EvaluationResult, staff: bool) -> dict:
    scores = s.scalars(select(CriterionScore).where(CriterionScore.result_id == r.id)).all()
    return ResultOut(
        result_id=r.id, submission_id=r.submission_id, status=r.status.value, total_score=r.total_score,
        summary=r.summary, rubric_id=r.rubric_id, created_at=r.created_at,
        # internal signals are staff-only
        overall_confidence=r.overall_confidence if staff else None, flags=r.flags if staff else None,
        model=r.model if staff else None, prompt_version=r.prompt_version if staff else None,
        criteria=[{
            "criterion_key": c.criterion_key, "score": c.score, "max_score": c.max_score,
            "confidence": c.confidence if staff else None, "rationale": c.rationale,
            "strengths": c.strengths, "improvements": c.improvements, "evidence": c.evidence,
        } for c in scores],
    ).model_dump(mode="json")


@app.get("/v1/evaluations/{job_id}/result")
def get_evaluation_result(job_id: str, user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    job = _get_job(s, user, job_id)
    r = s.scalar(select(EvaluationResult).where(EvaluationResult.job_id == job.id))
    staff = s.get(Submission, job.submission_id).student_id != user.id
    if not r:
        raise HTTPException(404 if job.status.value in ("failed", "cancelled") else 409,
                            f"no result yet (job status: {job.status.value})")
    if not staff and r.status != ResultStatus.approved:
        raise HTTPException(409, "result is under review and not yet released")
    return _result_out(s, r, staff)


@app.get("/v1/submissions/{submission_id}/results")
def list_results(submission_id: str, user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    """Full evaluation history (staff) or released results (student)."""
    sub = _get_submission(s, user, submission_id)
    staff = sub.student_id != user.id
    q = select(EvaluationResult).where(EvaluationResult.submission_id == sub.id).order_by(EvaluationResult.created_at)
    rows = s.scalars(q).all()
    return [_result_out(s, r, staff) for r in rows if staff or r.status == ResultStatus.approved]


# ------------------------------------------------------------------ rubrics


def _rubric_out(r: Rubric) -> dict:
    return RubricOut(rubric_id=r.id, assignment_id=r.assignment_id, version=r.version, is_active=r.is_active,
                     criteria=[{"key": c.key, "title": c.title, "description": c.description, "weight": c.weight,
                                "max_score": c.max_score, "scoring_guide": c.scoring_guide,
                                "evidence_sources": c.evidence_sources} for c in r.criteria]).model_dump(mode="json")


@app.put("/v1/assignments/{assignment_id}/rubric")
def update_rubric(assignment_id: str, body: RubricIn,
                  user: User = Depends(require_role(Role.instructor)), s: Session = Depends(db.get_session)):
    """Creates a NEW rubric version (published versions are immutable, so past
    results remain explainable). Existing results are not re-scored automatically;
    staff can force re-evaluation per submission or in bulk."""
    asg = s.get(Assignment, assignment_id)
    if not asg or not is_course_staff(s, user, asg.course_id):
        raise HTTPException(404, "assignment not found")
    version = (s.scalar(select(func.max(Rubric.version)).where(Rubric.assignment_id == asg.id)) or 0) + 1
    if body.publish:
        s.execute(update(Rubric).where(Rubric.assignment_id == asg.id, Rubric.is_active.is_(True))
                  .values(is_active=False))
    r = Rubric(assignment_id=asg.id, version=version, is_published=body.publish, is_active=body.publish,
               created_by=user.id)
    for i, c in enumerate(body.criteria):
        r.criteria.append(RubricCriterion(key=c.key, title=c.title, description=c.description, weight=c.weight,
                                          max_score=c.max_score, position=i,
                                          scoring_guide=[b.model_dump() for b in c.scoring_guide],
                                          evidence_sources=c.evidence_sources))
    s.add(r)
    s.flush()  # unique (assignment, version) + partial unique active index reject concurrent edits -> 409
    _audit(s, user, "rubric.versioned", "rubric", r.id, version=version)
    s.commit()
    return _rubric_out(r)


@app.get("/v1/assignments/{assignment_id}/rubric")
def get_rubric(assignment_id: str, user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    r = s.scalar(select(Rubric).where(Rubric.assignment_id == assignment_id, Rubric.is_active.is_(True)))
    if not r:
        raise HTTPException(404, "no active rubric")
    return _rubric_out(r)


# ------------------------------------------------------------------ human review


def _review_out(r: HumanReview) -> dict:
    return ReviewOut(review_id=r.id, result_id=r.result_id, status=r.status.value, reason=r.reason,
                     reviewer_id=r.reviewer_id, overrides=r.overrides, decision_comment=r.decision_comment,
                     created_at=r.created_at, decided_at=r.decided_at).model_dump(mode="json")


@app.post("/v1/results/{result_id}/reviews", status_code=201)
def request_review(result_id: str, body: ReviewRequest, user: User = Depends(current_user),
                   s: Session = Depends(db.get_session)):
    write_limiter.check(user.id)
    r = s.get(EvaluationResult, result_id)
    if not r:
        raise HTTPException(404, "result not found")
    sub = _get_submission(s, user, r.submission_id)
    is_student = sub.student_id == user.id
    if is_student:
        if r.status != ResultStatus.approved:
            raise HTTPException(409, "only released results can be disputed")
        prior = s.scalar(select(func.count()).select_from(HumanReview).where(
            HumanReview.result_id == r.id, HumanReview.source == "student"))
        if prior:
            raise HTTPException(409, "a re-review was already requested for this result")
    open_review = s.scalar(select(HumanReview).where(
        HumanReview.result_id == r.id, HumanReview.status.in_([ReviewStatus.requested, ReviewStatus.in_progress])))
    if open_review:
        return JSONResponse(status_code=200, content=_review_out(open_review))
    rev = HumanReview(result_id=r.id, requested_by=user.id, reason=body.reason,
                      source="student" if is_student else "staff")
    if not is_student:
        r.status = ResultStatus.pending_review  # staff pull it back from the student until decided
    s.add(rev)
    s.flush()
    _audit(s, user, "review.requested", "human_review", rev.id)
    s.commit()
    return _review_out(rev)


@app.get("/v1/reviews")
def review_queue(status_: str = Query("requested", alias="status"), limit: int = Query(50, le=200),
                 user: User = Depends(require_role(Role.evaluator, Role.instructor)),
                 s: Session = Depends(db.get_session)):
    q = (select(HumanReview).join(EvaluationResult, HumanReview.result_id == EvaluationResult.id)
         .join(Submission, EvaluationResult.submission_id == Submission.id)
         .join(Assignment, Submission.assignment_id == Assignment.id)
         .where(HumanReview.status == ReviewStatus(status_)).order_by(HumanReview.created_at).limit(limit))
    if user.role != Role.admin:
        q = q.join(CourseMembership, (CourseMembership.course_id == Assignment.course_id)
                   & (CourseMembership.user_id == user.id))
    return [_review_out(r) for r in s.scalars(q).all()]


def _staff_for_review(s: Session, user: User, rev: HumanReview) -> EvaluationResult:
    r = s.get(EvaluationResult, rev.result_id)
    sub = s.get(Submission, r.submission_id)
    course_id = s.scalar(select(Assignment.course_id).where(Assignment.id == sub.assignment_id))
    if not is_course_staff(s, user, course_id):
        raise HTTPException(404, "review not found")
    return r


@app.post("/v1/reviews/{review_id}/claim")
def claim_review(review_id: str, user: User = Depends(require_role(Role.evaluator, Role.instructor)),
                 s: Session = Depends(db.get_session)):
    rev = s.get(HumanReview, review_id)
    if not rev:
        raise HTTPException(404, "review not found")
    _staff_for_review(s, user, rev)
    res = s.execute(update(HumanReview).where(HumanReview.id == rev.id, HumanReview.status == ReviewStatus.requested)
                    .values(status=ReviewStatus.in_progress, reviewer_id=user.id))
    if res.rowcount != 1:
        raise HTTPException(409, "review already claimed or decided")
    s.commit()
    s.refresh(rev)
    return _review_out(rev)


@app.post("/v1/reviews/{review_id}/decision")
def decide_review(review_id: str, body: ReviewDecision,
                  user: User = Depends(require_role(Role.evaluator, Role.instructor)),
                  s: Session = Depends(db.get_session)):
    rev = s.get(HumanReview, review_id)
    if not rev:
        raise HTTPException(404, "review not found")
    r = _staff_for_review(s, user, rev)
    if rev.status == ReviewStatus.in_progress and rev.reviewer_id != user.id:
        raise HTTPException(409, "review is claimed by another reviewer")
    new_status = ReviewStatus.approved if body.decision == "approve" else ReviewStatus.rejected
    res = s.execute(update(HumanReview)
                    .where(HumanReview.id == rev.id,
                           HumanReview.status.in_([ReviewStatus.requested, ReviewStatus.in_progress]))
                    .values(status=new_status, reviewer_id=user.id, decision_comment=body.comment,
                            decided_at=utcnow()))
    if res.rowcount != 1:
        raise HTTPException(409, "review already decided")

    sub = s.get(Submission, r.submission_id)
    new_job = None
    if new_status == ReviewStatus.approved:
        rubric_weights = {c.id: c.weight for c in s.scalars(
            select(RubricCriterion).where(RubricCriterion.rubric_id == r.rubric_id))}
        scores = s.scalars(select(CriterionScore).where(CriterionScore.result_id == r.id)).all()
        applied = {}
        for c in scores:
            if o := body.overrides.get(c.criterion_key):
                new = float(o.get("score", c.score))
                if not 0 <= new <= c.max_score:
                    raise HTTPException(422, f"override for {c.criterion_key} outside 0-{c.max_score}")
                applied[c.criterion_key] = {"from": c.score, "to": new, "comment": o.get("comment", "")}
                c.score = new
        unknown = set(body.overrides) - {c.criterion_key for c in scores}
        if unknown:
            raise HTTPException(422, f"unknown criteria in overrides: {sorted(unknown)}")
        r.total_score = round(100 * sum(rubric_weights[c.criterion_id] * c.score / c.max_score for c in scores), 2)
        r.status = ResultStatus.approved
        rev.overrides = applied  # old and new values kept for audit
        sub.status = SubmissionStatus.finalized
    else:
        r.status, r.is_current = ResultStatus.rejected, False
        if body.re_evaluate:
            new_job = EvaluationJob(submission_id=sub.id, rubric_id=r.rubric_id, requested_by=user.id,
                                    idempotency_key=f"{sub.id}:{r.rubric_id}:review:{rev.id}", priority=10,
                                    max_attempts=settings.job_max_attempts)
            s.add(new_job)
    _audit(s, user, f"review.{new_status.value}", "human_review", rev.id, overrides=body.overrides)
    s.commit()
    s.refresh(rev)
    out = _review_out(rev)
    out["result_status"] = r.status.value
    out["total_score"] = r.total_score
    out["re_evaluation_job_id"] = new_job.id if new_job else None
    return out
