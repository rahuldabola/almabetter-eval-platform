import json

import pytest
from pydantic import BaseModel

from app.evaluation.engine import aggregate, verify_evidence
from app.ingest.chunker import Chunk
from app.llm.client import BudgetExceeded, CircuitBreaker, LLMClient
from app.llm.fake import FakeProvider
from app.llm.provider import (
    LLMBadRequest,
    LLMContextTooLarge,
    LLMMalformedOutput,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)


class Out(BaseModel):
    score: float


OK = json.dumps({"score": 7})


def client(script, **kw):
    sleeps = []
    c = LLMClient(FakeProvider(script), sleep=sleeps.append, **kw)
    return c, sleeps


def test_retries_transient_errors_then_succeeds():
    c, sleeps = client([LLMTimeout("t"), LLMUnavailable("503"), OK])
    assert c.structured("s", "p", Out).score == 7
    assert len(sleeps) == 2 and c.usage.calls == 1


def test_rate_limit_honours_retry_after():
    c, sleeps = client([LLMRateLimited("429", retry_after=12), OK])
    c.structured("s", "p", Out)
    assert sleeps[0] >= 12


def test_gives_up_after_max_retries():
    c, _ = client([LLMTimeout("t")] * 10, max_retries=3)
    with pytest.raises(LLMTimeout):
        c.structured("s", "p", Out)


def test_non_retryable_fails_fast():
    c, sleeps = client([LLMBadRequest("400"), OK])
    with pytest.raises(LLMBadRequest):
        c.structured("s", "p", Out)
    assert sleeps == []


def test_malformed_output_is_repaired_once():
    c, _ = client(["not json at all", "```json\n" + OK + "\n```"])
    assert c.structured("s", "p", Out).score == 7


def test_malformed_twice_raises():
    c, _ = client(["nope", '{"score": "high"}'])
    with pytest.raises(LLMMalformedOutput):
        c.structured("s", "p", Out)


def test_context_and_budget_guards():
    c, _ = client([OK])
    with pytest.raises(LLMContextTooLarge):
        c.structured("s", "x" * 4 * 200_000, Out)
    c, _ = client([OK], token_budget=1_000)
    with pytest.raises(BudgetExceeded):
        c.structured("s", "x" * 4 * 2_000, Out, max_output_tokens=10)


def test_circuit_breaker_switches_to_fallback():
    primary = FakeProvider([LLMUnavailable("down")] * 50)
    fallback = FakeProvider([OK])
    c = LLMClient(primary, fallback=fallback, max_retries=2, breaker=CircuitBreaker(threshold=2),
                  sleep=lambda _: None)
    assert c.structured("s", "p", Out).score == 7
    assert fallback.calls == 1


def test_hallucinated_evidence_is_not_verified():
    ch = Chunk("app/main.py", 1, 3, "from fastapi import FastAPI\napp = FastAPI()\n")
    out = verify_evidence([
        {"ref": "app/main.py:1-3", "quote": "app = FastAPI()"},
        {"ref": "app/main.py:1-3", "quote": "def login(): ..."},       # not in chunk
        {"ref": "secret/other.py:1-9", "quote": "app = FastAPI()"},    # chunk never offered
    ], {ch.ref: ch})
    assert [e["verified"] for e in out] == [True, False, False]


def test_review_routing():
    base = {"weight": 1.0, "score": 8, "max_score": 10, "confidence": 0.9, "flags": []}
    assert not aggregate([base], injection_refs=[], facts={}, threshold=0.6).needs_review
    agg = aggregate([base], injection_refs=["README.md:1-5"], facts={}, threshold=0.6)
    assert agg.needs_review and "prompt_injection_suspected" in agg.reasons
    agg = aggregate([{**base, "score": 5.2}], injection_refs=[], facts={}, threshold=0.6)
    assert "borderline_pass_fail" in agg.reasons
    agg = aggregate([{**base, "confidence": 0.3}], injection_refs=[], facts={}, threshold=0.6)
    assert agg.reasons == ["low_confidence:0.30"]


def test_gemini_retry_delay_parsed_from_body():
    import httpx

    from app.llm.provider import _retry_after

    body = '{"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}}'
    assert _retry_after(httpx.Response(429, text=body)) == 37
    assert _retry_after(httpx.Response(429, headers={"retry-after": "5"})) == 5
    assert _retry_after(httpx.Response(429, text="{}")) is None


def test_long_quota_wait_is_not_slept_through():
    c, sleeps = client([LLMRateLimited("daily quota", retry_after=7 * 3600), OK])
    with pytest.raises(LLMRateLimited):
        c.structured("s", "p", Out)
    assert sleeps == []  # the worker is released immediately; the job is parked instead


def test_retry_in_message_parsed():
    import httpx

    from app.llm.provider import _retry_after

    assert _retry_after(httpx.Response(429, text='{"message": "Please retry in 7h17m59.5s."}')) == 7 * 3600 + 17 * 60 + 59.5
