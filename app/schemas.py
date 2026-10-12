"""Request/response contracts for the public API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import ArtifactKind


class ArtifactIn(BaseModel):
    kind: ArtifactKind
    source_url: str | None = Field(default=None, max_length=2048)
    filename: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self):
        link_kinds = {ArtifactKind.github, ArtifactKind.drive, ArtifactKind.live_url}
        if self.kind in link_kinds and not self.source_url:
            raise ValueError(f"{self.kind.value} artifact requires source_url")
        if self.kind not in link_kinds and not self.filename:
            raise ValueError(f"{self.kind.value} artifact requires filename (it is uploaded)")
        return self

    @field_validator("filename")
    @classmethod
    def _filename(cls, v):
        if v and (("/" in v) or ("\\" in v) or v.startswith(".")):
            raise ValueError("filename must be a plain file name")
        return v


class SubmissionCreate(BaseModel):
    assignment_id: str
    notes: str = Field(default="", max_length=5000)
    artifacts: list[ArtifactIn] = Field(min_length=1, max_length=10)


class ArtifactOut(BaseModel):
    id: str
    kind: ArtifactKind
    status: str
    source_url: str | None
    filename: str | None
    size_bytes: int | None
    sha256: str | None
    upload_url: str | None = None  # presigned S3 PUT URL in production


class SubmissionOut(BaseModel):
    id: str
    assignment_id: str
    student_id: str
    attempt_no: int
    status: str
    created_at: datetime
    artifacts: list[ArtifactOut]
    latest_job_id: str | None = None


class EvaluationStart(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    force: bool = False  # staff-only: re-evaluate even if a result exists (e.g. new rubric)


class JobOut(BaseModel):
    job_id: str
    submission_id: str
    rubric_id: str
    status: str
    current_stage: str | None
    progress: int
    attempts: int
    max_attempts: int
    last_error_code: str | None
    created_at: datetime
    finished_at: datetime | None


class CriterionScoreOut(BaseModel):
    criterion_key: str
    score: float
    max_score: float
    confidence: float | None = None
    rationale: str
    strengths: list[str]
    improvements: list[str]
    evidence: list[dict]


class ResultOut(BaseModel):
    result_id: str
    submission_id: str
    status: str
    total_score: float
    overall_confidence: float | None = None
    summary: str
    flags: list[str] | None = None
    model: str | None = None
    prompt_version: str | None = None
    rubric_id: str
    criteria: list[CriterionScoreOut]
    created_at: datetime


class BandIn(BaseModel):
    min: float
    max: float
    descriptor: str = Field(max_length=500)


class CriterionIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    title: str = Field(max_length=200)
    description: str = Field(max_length=2000)
    weight: float = Field(gt=0, le=1)
    max_score: float = Field(default=10, gt=0, le=100)
    scoring_guide: list[BandIn] = []
    evidence_sources: list[str] = []


class RubricIn(BaseModel):
    criteria: list[CriterionIn] = Field(min_length=1, max_length=20)
    publish: bool = True

    @model_validator(mode="after")
    def _check(self):
        total = sum(c.weight for c in self.criteria)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"criterion weights must sum to 1.0 (got {total:.3f})")
        keys = [c.key for c in self.criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("criterion keys must be unique")
        for c in self.criteria:
            for b in c.scoring_guide:
                if not (0 <= b.min <= b.max <= c.max_score):
                    raise ValueError(f"band {b.min}-{b.max} outside 0-{c.max_score} for {c.key}")
        return self


class RubricOut(BaseModel):
    rubric_id: str
    assignment_id: str
    version: int
    is_active: bool
    criteria: list[CriterionIn]


class ReviewRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class ReviewDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(min_length=3, max_length=5000)
    # approve may carry score overrides: {"code_quality": {"score": 7, "comment": "..."}}
    overrides: dict[str, dict] = {}
    re_evaluate: bool = False  # on reject: queue a fresh AI evaluation


class ReviewOut(BaseModel):
    review_id: str
    result_id: str
    status: str
    reason: str
    reviewer_id: str | None
    overrides: dict
    decision_comment: str | None
    created_at: datetime
    decided_at: datetime | None
