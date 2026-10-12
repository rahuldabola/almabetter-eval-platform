"""Deterministic offline provider used by tests, CI and the no-API-key demo.

It reads the same prompts the real model gets and produces schema-valid JSON
using simple heuristics, so the whole pipeline (retrieval, evidence
verification, aggregation, review routing) is exercised without network access.
It can also be scripted to fail, to test retry/repair paths.
"""
from __future__ import annotations

import json
import re

from app.llm.provider import LLMError, LLMResponse

CHUNK_RE = re.compile(r'<chunk ref="(?P<ref>[^"]+)">\n(?P<body>.*?)\n</chunk>', re.S)
FACTS_RE = re.compile(r"<facts>\n(.*?)\n</facts>", re.S)
CRITERION_RE = re.compile(r'<criterion key="(?P<key>[^"]+)" max_score="(?P<max>[\d.]+)">', re.S)


class FakeProvider:
    name = "fake"
    context_window = 128_000
    model = "fake-deterministic-v1"

    def __init__(self, script: list[LLMError | str] | None = None):
        # Each scripted item is consumed by one call: an exception is raised,
        # a string is returned verbatim (e.g. malformed JSON).
        self.script = list(script or [])
        self.calls = 0

    def complete(self, system: str, prompt: str, *, temperature: float = 0.0, json_mode: bool = True) -> LLMResponse:
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return self._resp(prompt, item)
        task = re.search(r"^TASK: (\w+)", prompt, re.M)
        task = task.group(1) if task else ""
        if task == "file_summaries":
            out = self._file_summaries(prompt)
        elif task == "project_summary":
            out = self._project_summary(prompt)
        elif task == "criterion":
            out = self._criterion(prompt)
        else:
            out = {"note": "unknown task"}
        return self._resp(prompt, json.dumps(out))

    def _resp(self, prompt: str, text: str) -> LLMResponse:
        return LLMResponse(text, len(prompt) // 4, len(text) // 4, self.model)

    @staticmethod
    def _file_summaries(prompt: str) -> dict:
        out = []
        for m in CHUNK_RE.finditer(prompt):
            path = m.group("ref").rsplit(":", 1)[0]
            first = next((ln.strip() for ln in m.group("body").splitlines() if ln.strip()), "")
            out.append({"path": path, "summary": f"{path}: starts with `{first[:80]}`", "role": "source"})
        return {"files": out}

    @staticmethod
    def _project_summary(prompt: str) -> dict:
        facts = json.loads(FACTS_RE.search(prompt).group(1)) if FACTS_RE.search(prompt) else {}
        lang = facts.get("primary_language") or "unknown"
        return {
            "purpose": f"A {lang} project submitted for evaluation.",
            "architecture": "Inferred from file layout by the offline provider.",
            "components": [],
            "tech_stack": list(facts.get("lines_of_code_by_language", {}).keys()),
            "risks": ["committed secrets"] if facts.get("possible_committed_secrets") else [],
        }

    @staticmethod
    def _criterion(prompt: str) -> dict:
        crit = CRITERION_RE.search(prompt)
        key, max_score = crit.group("key"), float(crit.group("max"))
        facts = json.loads(FACTS_RE.search(prompt).group(1)) if FACTS_RE.search(prompt) else {}
        chunks = list(CHUNK_RE.finditer(prompt))
        ratio = min(1.0, 0.4 + 0.1 * len(chunks))
        if "test" in key and not facts.get("test_files"):
            ratio = 0.2
        if "doc" in key and not facts.get("has_readme"):
            ratio = 0.3
        evidence = []
        for m in chunks[:2]:
            line = next((ln for ln in m.group("body").splitlines() if ln.strip()), "")
            evidence.append({"ref": m.group("ref"), "quote": line.strip()[:120]})
        return {
            "score": round(max_score * ratio, 1),
            "confidence": 0.8 if len(chunks) >= 2 else 0.5,
            "rationale": f"Offline heuristic based on {len(chunks)} retrieved evidence chunks and project facts.",
            "strengths": ["Relevant evidence found"] if chunks else [],
            "improvements": [] if chunks else ["No evidence found for this criterion"],
            "evidence": evidence,
        }
