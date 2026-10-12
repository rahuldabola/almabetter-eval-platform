# AI-Powered Project Evaluation Platform: Backend Architecture + PoC

Submission for the **AlmaBetter SDE-II Backend Architecture Assignment**.

- **The design document (main deliverable):** [`docs/DESIGN.md`](docs/DESIGN.md) (PDF: [`docs/DESIGN.pdf`](docs/DESIGN.pdf)).
  It covers architecture, the submission pipeline, APIs, schema, LLM architecture, failure handling, security, scaling from 30 to 10k/day, deployment, technology research with sources, and the roadmap.
- **The proof of concept (optional deliverable):** a working backbone of that design. Rubric-driven AI evaluation, an idempotent job pipeline with crash recovery, hardened ingestion of untrusted uploads, and a human-review workflow. 53 tests.

```
POST /v1/submissions ─▶ upload ─▶ POST /evaluations (×5 → 1 job) ─▶ worker: claim(lease)
  ─▶ safe extract ─▶ inventory + facts ─▶ map/reduce understanding ─▶ per-criterion RAG + LLM
  ─▶ verify cited evidence ─▶ aggregate ─▶ auto-publish or human review ─▶ student result
```

## Quick start

```bash
pip install -r requirements-dev.txt
pytest -q                          # 51 passed, 2 skipped (the 2 need PostgreSQL)
python -m scripts.demo             # full end-to-end flow with the offline deterministic LLM
```

Real model:
```bash
LLM_PROVIDER=gemini GEMINI_API_KEY=... GEMINI_MODEL=gemini-flash-lite-latest python -m scripts.demo
```
A recorded live run is in [`docs/sample_run_gemini.txt`](docs/sample_run_gemini.txt): 8 LLM calls, ~18k tokens, 14 s, 18/18 evidence citations verified.

PostgreSQL (exercises `FOR UPDATE SKIP LOCKED`, partial unique indexes and real races):
```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/db pytest -q    # 53 passed
```

Full stack (NGINX → API, 2 workers, Postgres + pgvector):
```bash
cp .env.example .env && docker compose up --build      # http://localhost:8080/healthz
docker compose exec api python -m app.seed             # demo users + JWTs + rubric
```

## What's demonstrated

| Concern | Where | Test |
|---|---|---|
| `POST /evaluations` ×5 creates 1 job (and ×10 in parallel on Postgres) | `app/api.py: start_evaluation` | `test_start_evaluation_five_times_creates_one_job`, `test_parallel_start_evaluation_creates_exactly_one_job` |
| Only one worker can claim a job | `app/jobqueue.py: claim_next` | `test_only_one_worker_can_claim`, `test_parallel_workers_never_double_claim` |
| Worker crash → lease reaped → retried; zombie worker fenced | `jobqueue.reap_expired_leases`, `heartbeat` | `test_crashed_worker_lease_is_reaped_and_zombie_is_fenced` |
| LLM outage → retry later → **resume from checkpoint** (no re-billing) | `app/worker.py: run_job` | `test_llm_outage_retries_and_resumes_from_checkpoint` |
| Daily quota → job parked until reset, attempt not consumed | `llm/client.py`, `jobqueue.fail` | `test_quota_exhaustion_parks_job_without_burning_attempts` |
| Malformed LLM JSON → repair turn; timeouts/429/5xx → backoff; breaker → fallback | `app/llm/client.py` | `tests/test_llm.py` |
| Hallucinated evidence rejected; scores clamped/capped | `engine.verify_evidence` | `test_hallucinated_evidence_is_not_verified` |
| Prompt injection → forced human review | `engine.scan_prompt_injection` | `test_prompt_injection_forces_human_review` |
| Zip Slip, zip bombs, symlinks, corrupt archives, SSRF | `app/ingest/` | `tests/test_ingest.py` |
| Tenancy isolation, RBAC, upload magic bytes | `app/auth.py`, `app/api.py` | `tests/test_api.py` |
| Rubric as versioned data; review claim/approve/override/reject/re-evaluate | `app/api.py` | `test_rubric_versioning`, `test_full_flow_with_review_gate` |

## PoC vs production

The PoC uses local disk instead of S3, DB polling instead of SQS, HS256 instead of IdP-issued RS256, BM25 instead of pgvector hybrid retrieval, and in-process rate limits instead of Redis. Each substitution sits behind the same interface the design describes. See `docs/DESIGN.md` §11.1.

## Use of AI tools

Claude (via Claude Code) was used to research and draft the document and to write the PoC and its tests. Gemini is used *as the system's LLM* in the live run. Details are in `docs/DESIGN.md` §12.
