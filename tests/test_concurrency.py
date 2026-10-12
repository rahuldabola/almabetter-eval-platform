"""Real races. Needs PostgreSQL (SQLite serialises writers, so it can't show them):
    TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/test_concurrency.py
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app import db
from app.auth import evaluation_limiter
from app.jobqueue import claim_next
from app.models import EvaluationJob

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs PostgreSQL")


def test_parallel_start_evaluation_creates_exactly_one_job(env, submit):
    sub = submit()
    c, h = env["client"], env["h"]["student"]
    barrier = threading.Barrier(10)

    def call(_):
        barrier.wait()
        return c.post(f"/v1/submissions/{sub['id']}/evaluations", headers=h)

    with ThreadPoolExecutor(10) as ex:
        responses = list(ex.map(call, range(10)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(202) == 1 and all(code in (200, 202, 409) for code in codes), codes
    with db.SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(EvaluationJob)) == 1


def test_parallel_workers_never_double_claim(env, submit):
    subs = [submit() for _ in range(6)]
    for sub in subs:
        evaluation_limiter.hits.clear()  # 6 evaluations/hour exceeds the per-student limit
        r = env["client"].post(f"/v1/submissions/{sub['id']}/evaluations", headers=env["h"]["student"])
        assert r.status_code == 202
    barrier = threading.Barrier(5)

    def worker(i):
        barrier.wait()
        got = []
        while True:
            with db.SessionLocal() as s:
                job = claim_next(s, f"w{i}", 60)
            if not job:
                return got
            got.append(job.id)

    with ThreadPoolExecutor(5) as ex:
        claimed = [j for lst in ex.map(worker, range(5)) for j in lst]
    assert len(claimed) == 6 and len(set(claimed)) == 6
