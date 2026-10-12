"""Relational schema. PostgreSQL in production; the PoC also runs on SQLite.

Design rules that keep state consistent:
  * Status columns only change through guarded UPDATEs (`WHERE status = <expected>`),
    so two workers can never both move the same row forward.
  * A partial unique index allows at most ONE active evaluation job per
    (submission, rubric version). This is the database-level backstop for idempotency.
  * Rubrics are versioned and immutable once published; results point at the exact
    rubric version they were scored against.
  * Results are append-only. A re-evaluation creates a new result and flips
    `is_current`; nothing is overwritten, so history is preserved.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base


class UTCDateTime(TypeDecorator):
    """Always hand out timezone-aware UTC datetimes (SQLite drops the offset)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def _id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    student = "student"
    evaluator = "evaluator"
    instructor = "instructor"
    admin = "admin"


class SubmissionStatus(str, enum.Enum):
    draft = "draft"            # created, artifacts still being attached/uploaded
    submitted = "submitted"    # frozen, ready for evaluation
    evaluating = "evaluating"
    evaluated = "evaluated"    # AI result exists (may still be pending review)
    finalized = "finalized"    # human-approved or auto-published result
    failed = "failed"


class ArtifactKind(str, enum.Enum):
    github = "github"
    zip = "zip"
    pdf = "pdf"
    drive = "drive"
    video = "video"
    document = "document"
    live_url = "live_url"


class ArtifactStatus(str, enum.Enum):
    pending_upload = "pending_upload"
    stored = "stored"
    rejected = "rejected"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    retry_wait = "retry_wait"
    succeeded = "succeeded"
    failed = "failed"          # permanent failure or attempts exhausted -> DLQ
    cancelled = "cancelled"


ACTIVE_JOB_STATUSES = (JobStatus.queued, JobStatus.running, JobStatus.retry_wait)


class ResultStatus(str, enum.Enum):
    ai_draft = "ai_draft"              # produced by AI, not visible to student yet
    pending_review = "pending_review"  # flagged or requested for human review
    approved = "approved"              # visible to student
    rejected = "rejected"              # reviewer rejected; a new result supersedes it
    superseded = "superseded"


class ReviewStatus(str, enum.Enum):
    requested = "requested"
    in_progress = "in_progress"
    approved = "approved"
    rejected = "rejected"


# --------------------------------------------------------------------------- people


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    title: Mapped[str] = mapped_column(String(200))


class CourseMembership(Base):
    """Tenancy boundary: a user only sees courses they belong to."""

    __tablename__ = "course_memberships"
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False))
    __table_args__ = (Index("ix_membership_user", "user_id"),)


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    project_type: Mapped[str] = mapped_column(String(50), default="web_app")  # drives pipeline profile
    allowed_artifact_kinds: Mapped[list] = mapped_column(JSON, default=lambda: [k.value for k in ArtifactKind])
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (Index("ix_assignment_course", "course_id"),)


# --------------------------------------------------------------------------- rubrics


class Rubric(Base):
    """Versioned rubric. Published versions are immutable; editing creates a new version."""

    __tablename__ = "rubrics"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id"))
    version: Mapped[int] = mapped_column(Integer)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    criteria: Mapped[list["RubricCriterion"]] = relationship(
        back_populates="rubric", order_by="RubricCriterion.position", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("assignment_id", "version", name="uq_rubric_version"),
        # exactly one active rubric per assignment
        Index(
            "uq_rubric_active",
            "assignment_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))          # stable machine key, e.g. "code_quality"
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(Float)            # weights of a rubric sum to 1.0
    max_score: Mapped[float] = mapped_column(Float, default=10)
    # Band descriptors make LLM scoring anchored instead of free-form:
    # [{"min": 8, "max": 10, "descriptor": "..."}, ...]
    scoring_guide: Mapped[list] = mapped_column(JSON, default=list)
    # Which evidence the analyser should retrieve: code | docs | architecture | tests | video | live_app
    evidence_sources: Mapped[list] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    rubric: Mapped[Rubric] = relationship(back_populates="criteria")
    __table_args__ = (UniqueConstraint("rubric_id", "key", name="uq_criterion_key"),)


# --------------------------------------------------------------------------- submissions


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id"))
    student_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)  # resubmissions are new rows
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus, native_enum=False), default=SubmissionStatus.draft
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    artifacts: Mapped[list["SubmissionArtifact"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("student_id", "idempotency_key", name="uq_submission_idem"),
        UniqueConstraint("assignment_id", "student_id", "attempt_no", name="uq_submission_attempt"),
        Index("ix_submission_student_created", "student_id", "created_at"),
        Index("ix_submission_assignment_status", "assignment_id", "status"),
    )


class SubmissionArtifact(Base):
    __tablename__ = "submission_artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"))
    kind: Mapped[ArtifactKind] = mapped_column(Enum(ArtifactKind, native_enum=False))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # GitHub / Drive / live URL
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # S3 key of raw upload
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ArtifactStatus] = mapped_column(
        Enum(ArtifactStatus, native_enum=False), default=ArtifactStatus.pending_upload
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (Index("ix_artifact_submission", "submission_id"), Index("ix_artifact_sha", "sha256"))


# --------------------------------------------------------------------------- evaluation


class EvaluationJob(Base):
    """One request to evaluate a submission against a specific rubric version.
    Doubles as the work-queue record (lease columns) - the DB is the source of truth
    for job state, the message broker only carries wake-ups."""

    __tablename__ = "evaluation_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, native_enum=False), default=JobStatus.queued)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = sooner
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100 for the UI
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        # At most one in-flight job per submission+rubric version.
        Index(
            "uq_job_active",
            "submission_id",
            "rubric_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running','retry_wait')"),
            sqlite_where=text("status IN ('queued','running','retry_wait')"),
        ),
        # The claim query: next runnable job by priority.
        Index("ix_job_claim", "status", "run_after", "priority"),
        Index("ix_job_lease", "status", "lease_expires_at"),
        Index("ix_job_submission", "submission_id"),
    )


class EvaluationAttempt(Base):
    """Every execution of a job (including crashed ones) - the audit trail for retries."""

    __tablename__ = "evaluation_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("evaluation_jobs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|succeeded|failed|lost
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (UniqueConstraint("job_id", "attempt_no", name="uq_attempt_no"),)


class StageCheckpoint(Base):
    """Output of a completed pipeline stage. A retried job resumes from the last
    checkpoint instead of re-paying for extraction and earlier LLM calls."""

    __tablename__ = "stage_checkpoints"
    job_id: Mapped[str] = mapped_column(ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(50), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)  # small outputs inline; large ones -> S3 key
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("evaluation_jobs.id"), unique=True)  # one result per job
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    rubric_id: Mapped[str] = mapped_column(ForeignKey("rubrics.id"))
    status: Mapped[ResultStatus] = mapped_column(Enum(ResultStatus, native_enum=False))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    total_score: Mapped[float] = mapped_column(Float)   # 0-100 weighted
    overall_confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    flags: Mapped[list] = mapped_column(JSON, default=list)   # e.g. ["prompt_injection_suspected"]
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    criterion_scores: Mapped[list["CriterionScore"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (
        Index("ix_result_submission_current", "submission_id", "is_current"),
        Index("ix_result_status_created", "status", "created_at"),  # reviewer queue
    )


class CriterionScore(Base):
    __tablename__ = "criterion_scores"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    result_id: Mapped[str] = mapped_column(ForeignKey("evaluation_results.id", ondelete="CASCADE"))
    criterion_id: Mapped[str] = mapped_column(ForeignKey("rubric_criteria.id"))
    criterion_key: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    max_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    strengths: Mapped[list] = mapped_column(JSON, default=list)
    improvements: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{"path":..., "quote":..., "verified": bool}]
    __table_args__ = (UniqueConstraint("result_id", "criterion_id", name="uq_score_criterion"),)


class HumanReview(Base):
    __tablename__ = "human_reviews"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    result_id: Mapped[str] = mapped_column(ForeignKey("evaluation_results.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(20), default="staff")  # auto | student | staff
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus, native_enum=False), default=ReviewStatus.requested)
    reason: Mapped[str] = mapped_column(Text)
    # Reviewer overrides: {"criterion_key": {"score": 7, "comment": "..."}}
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        # only one open review per result
        Index(
            "uq_review_open",
            "result_id",
            unique=True,
            postgresql_where=text("status IN ('requested','in_progress')"),
            sqlite_where=text("status IN ('requested','in_progress')"),
        ),
        Index("ix_review_status", "status", "created_at"),
    )


class IdempotencyRecord(Base):
    """Stores the first response for an Idempotency-Key so retried POSTs replay it."""

    __tablename__ = "idempotency_records"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)
