"""Reliability wrapper around a provider. All LLM calls in the engine go through here.

  * Retries transient errors (timeout, 429, 5xx) with exponential backoff + full jitter,
    honouring Retry-After. Waits longer than `max_inline_wait_s` are not slept through:
    the error propagates and the job is re-queued for when the quota resets.
  * Validates every response against a Pydantic schema; on malformed output it
    re-asks once with the validation error (a "repair" turn), then gives up.
  * Enforces a per-job token budget and a pre-flight context-size check.
  * Circuit breaker: after N consecutive provider failures it fails fast so jobs
    are parked in retry_wait instead of hammering a dead dependency.
  * Optional fallback provider when the primary is unavailable.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.ingest.chunker import estimate_tokens
from app.llm.provider import (
    LLMContextTooLarge,
    LLMError,
    LLMMalformedOutput,
    LLMProvider,
    LLMRateLimited,
    LLMUnavailable,
)

log = logging.getLogger("llm")
_sleep = time.sleep  # module-level so tests can patch it
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(LLMError):
    code = "token_budget_exceeded"
    retryable = False


class CircuitOpen(LLMUnavailable):
    code = "llm_circuit_open"


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.opened_at and time.monotonic() - self.opened_at < self.cooldown_s:
                raise CircuitOpen("circuit open: provider failing, try later")
            if self.opened_at:  # half-open: let one call through
                self.opened_at = None

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self.failures = 0
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = time.monotonic()


def _extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences or add prose; take the object."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start : end + 1] if start != -1 and end > start else t


@dataclass
class LLMClient:
    provider: LLMProvider
    fallback: LLMProvider | None = None
    max_retries: int = 4
    token_budget: int = 400_000
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    max_inline_wait_s: float = 60.0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    sleep: Callable[[float], None] = lambda d: _sleep(d)
    usage: Usage = field(default_factory=Usage)
    on_call: Callable[[], None] | None = None  # e.g. worker heartbeat / lease check

    @property
    def model_name(self) -> str:
        return getattr(self.provider, "model", self.provider.name)

    def structured(self, system: str, prompt: str, schema: type[T], *, max_output_tokens: int = 4000) -> T:
        need = estimate_tokens(system) + estimate_tokens(prompt) + max_output_tokens
        if need > self.provider.context_window:
            raise LLMContextTooLarge(f"prompt needs ~{need} tokens > window {self.provider.context_window}")
        if self.usage.total + need > self.token_budget:
            raise BudgetExceeded(f"job token budget {self.token_budget} would be exceeded")

        text = self._call_with_retries(system, prompt)
        try:
            return schema.model_validate_json(_extract_json(text))
        except ValidationError as e:
            log.warning("malformed LLM output, attempting repair: %s", e.errors()[:3])
            repair = (
                f"{prompt}\n\nYour previous answer was invalid:\n{str(e)[:1500]}\n"
                f"Return ONLY a JSON object matching this schema:\n{json.dumps(schema.model_json_schema())}"
            )
            text = self._call_with_retries(system, repair)
            try:
                return schema.model_validate_json(_extract_json(text))
            except ValidationError as e2:
                raise LLMMalformedOutput(f"output failed validation after repair: {e2.errors()[:3]}") from e2

    def _call_with_retries(self, system: str, prompt: str) -> str:
        attempt = 0
        provider = self.provider
        while True:
            if self.on_call:
                self.on_call()
            primary = provider is self.provider
            try:
                if primary:
                    self.breaker.check()
                resp = provider.complete(system, prompt)
                if primary:
                    self.breaker.record(True)
                self.usage.tokens_in += resp.tokens_in
                self.usage.tokens_out += resp.tokens_out
                self.usage.calls += 1
                return resp.text
            except CircuitOpen:
                if self.fallback and provider is not self.fallback:
                    log.warning("primary circuit open; switching to fallback provider")
                    provider = self.fallback
                    continue
                raise
            except LLMError as e:
                if primary and e.retryable:
                    self.breaker.record(False)
                if not e.retryable:
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    if self.fallback and provider is not self.fallback and isinstance(e, LLMUnavailable):
                        provider, attempt = self.fallback, 0
                        continue
                    raise
                delay = random.uniform(0, min(self.max_delay_s, self.base_delay_s * 2**attempt))  # full jitter
                if isinstance(e, LLMRateLimited) and e.retry_after:
                    if e.retry_after > self.max_inline_wait_s:
                        # e.g. a daily quota ("retry in 7h"): don't hold a worker and a lease for
                        # hours - surface it so the job is parked until the quota resets.
                        raise
                    delay = max(delay, e.retry_after)
                log.info("LLM %s, retry %d/%d in %.1fs", e.code, attempt, self.max_retries, delay)
                self.sleep(delay)
