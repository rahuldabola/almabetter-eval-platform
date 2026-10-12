"""Provider abstraction. The evaluation engine depends on `LLMProvider`, never on a
vendor SDK, so the model/vendor can be swapped (Bedrock, OpenAI, Gemini,
self-hosted vLLM) or a fallback provider used during an outage."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


class LLMError(Exception):
    code = "llm_error"
    retryable = False


class LLMTimeout(LLMError):
    code = "llm_timeout"
    retryable = True


class LLMRateLimited(LLMError):
    code = "llm_rate_limited"
    retryable = True

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMUnavailable(LLMError):
    code = "llm_unavailable"
    retryable = True


class LLMContextTooLarge(LLMError):
    code = "llm_context_too_large"
    retryable = False  # retrying the same prompt is pointless; caller must shrink it


class LLMBadRequest(LLMError):
    code = "llm_bad_request"


class LLMMalformedOutput(LLMError):
    code = "llm_malformed_output"
    retryable = True


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    model: str


class LLMProvider(Protocol):
    name: str
    context_window: int

    def complete(self, system: str, prompt: str, *, temperature: float = 0.0, json_mode: bool = True) -> LLMResponse: ...


def _retry_after(r: httpx.Response) -> float | None:
    """Retry-After header, or Gemini's RetryInfo in the body ("retryDelay": "37s")."""
    ra = r.headers.get("retry-after")
    if ra and ra.isdigit():
        return float(ra)
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', r.text)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", r.text)  # "retry in 7h17m59.1s"
    if m and any(m.groups()):
        h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + sec
    return None


class GeminiProvider:
    """Gemini REST API (generateContent). JSON mode + server-side validation in client.py."""

    name = "gemini"
    context_window = 1_000_000

    def __init__(self, api_key: str, model: str, timeout_s: int = 60):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for the gemini provider")
        self.model = model
        self._client = httpx.Client(timeout=timeout_s)
        self._url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        self._key = api_key

    def complete(self, system: str, prompt: str, *, temperature: float = 0.0, json_mode: bool = True) -> LLMResponse:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, **({"responseMimeType": "application/json"} if json_mode else {})},
        }
        try:
            r = self._client.post(self._url, json=body, headers={"x-goog-api-key": self._key})
        except httpx.TimeoutException as e:
            raise LLMTimeout(str(e)) from e
        except httpx.TransportError as e:
            raise LLMUnavailable(str(e)) from e
        if r.status_code == 429:
            raise LLMRateLimited("rate limited", _retry_after(r))
        if r.status_code >= 500:
            raise LLMUnavailable(f"provider returned {r.status_code}")
        if r.status_code == 400 and re.search(r"token|too long|exceeds", r.text, re.I):
            raise LLMContextTooLarge(r.text[:300])
        if r.status_code >= 400:
            raise LLMBadRequest(f"{r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError) as e:
            # Safety block / empty candidate: treat as malformed so it is retried once, then surfaced.
            raise LLMMalformedOutput(f"no content in response: {json.dumps(data)[:300]}") from e
        usage = data.get("usageMetadata", {})
        return LLMResponse(text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0), self.model)
