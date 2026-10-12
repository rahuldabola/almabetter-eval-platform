"""End-to-end walkthrough against an in-process API + worker.

    python -m scripts.demo                      # offline, deterministic fake LLM
    LLM_PROVIDER=gemini GEMINI_API_KEY=... python -m scripts.demo   # real model

Shows: submission -> upload -> start evaluation 5x (one job) -> worker run ->
status -> result -> human review with override.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="evaldemo-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{(TMP / 'demo.db').as_posix()}")
os.environ.setdefault("STORAGE_DIR", str(TMP / "storage"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.api import app  # noqa: E402
from app.seed import seed  # noqa: E402
from app.worker import process_one  # noqa: E402


def zip_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    return buf.getvalue()


def show(title: str, data) -> None:
    print(f"\n=== {title}")
    print(json.dumps(data, indent=2)[:3000])


def main() -> None:
    db.init_db()
    with db.SessionLocal() as s:
        ids = seed(s)
    T = ids["tokens"]
    student = {"Authorization": f"Bearer {T['student']}"}
    reviewer = {"Authorization": f"Bearer {T['evaluator']}"}
    c = TestClient(app)

    r = c.post("/v1/submissions", headers={**student, "Idempotency-Key": "demo-sub-1"}, json={
        "assignment_id": ids["assignment_id"], "notes": "My capstone",
        "artifacts": [{"kind": "zip", "filename": "project.zip"}],
    })
    sub = r.json()
    show(f"POST /v1/submissions -> {r.status_code}", sub)

    art = sub["artifacts"][0]
    payload = zip_dir(ROOT / "samples" / "task-manager-api")
    r = c.post(art["upload_url"], headers=student, files={"file": ("project.zip", payload, "application/zip")})
    show(f"upload {len(payload)} bytes -> {r.status_code}", r.json()["artifacts"][0])

    job_ids = set()
    for i in range(5):
        r = c.post(f"/v1/submissions/{sub['id']}/evaluations", headers=student)
        job_ids.add(r.json()["job_id"])
        print(f"POST /evaluations call {i + 1}: HTTP {r.status_code} job={r.json()['job_id']}")
    print(f"-> distinct jobs created: {len(job_ids)}")
    job_id = job_ids.pop()

    # Run the worker until the job is terminal. With a real provider, a rate-limit
    # storm parks the job in retry_wait; we wait out run_after and the next attempt
    # resumes from the last checkpoint (completed stages are not re-run).
    from app.models import EvaluationJob, utcnow

    for _ in range(10):
        process_one("demo-worker")
        with db.SessionLocal() as s:
            job = s.get(EvaluationJob, job_id)
            if job.status.value in ("succeeded", "failed"):
                break
            wait = max(0.0, (job.run_after - utcnow()).total_seconds())
            print(f"job {job.status.value} at stage {job.current_stage} ({job.last_error_code}); "
                  f"resuming from checkpoint in {wait:.0f}s")
        time.sleep(min(wait, 120) + 1)

    show("GET /v1/evaluations/{id}", c.get(f"/v1/evaluations/{job_id}", headers=student).json())
    staff_view = c.get(f"/v1/evaluations/{job_id}/result", headers=reviewer).json()
    show("GET result (evaluator view)", staff_view)
    r = c.get(f"/v1/evaluations/{job_id}/result", headers=student)
    show(f"GET result (student view) -> {r.status_code}", r.json())

    queue = c.get("/v1/reviews", headers=reviewer).json()
    if not queue:
        r = c.post(f"/v1/results/{staff_view['result_id']}/reviews", headers=reviewer,
                   json={"reason": "Spot-check of an AI evaluation"})
        queue = [r.json()]
    rev = queue[0]
    show("review queue item", rev)
    c.post(f"/v1/reviews/{rev['review_id']}/claim", headers=reviewer)
    r = c.post(f"/v1/reviews/{rev['review_id']}/decision", headers=reviewer, json={
        "decision": "approve", "comment": "Checked the code; documentation deserves more credit.",
        "overrides": {"documentation": {"score": 9, "comment": "README covers setup, endpoints and design"}},
    })
    show(f"POST decision -> {r.status_code}", r.json())
    r = c.get(f"/v1/evaluations/{job_id}/result", headers=student)
    print(f"\nstudent can now see the result: HTTP {r.status_code}, total={r.json().get('total_score')}")
    print(f"\n(demo db + storage in {TMP})")


if __name__ == "__main__":
    main()
