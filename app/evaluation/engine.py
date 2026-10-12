"""The evaluation engine: pure functions over an extracted project + a rubric.
No database access here - the worker persists checkpoints between stages.

Stages
  1. inventory    deterministic walk/classify/facts               (no LLM)
  2. understand   rank files -> map: batched file summaries        (cheap model)
                  reduce: one project summary                      (cheap model)
  3. evaluate     per criterion: retrieve evidence -> score with
                  structured output -> verify cited evidence       (strong model)
  4. aggregate    weighted total, confidence, review routing       (no LLM)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.evaluation import prompts
from app.evaluation.retrieval import BM25Index, categories_for
from app.ingest.chunker import Chunk, chunk_text, estimate_tokens
from app.ingest.classify import Inventory
from app.llm.client import LLMClient

TEXT_CATEGORIES = {"code", "test", "docs", "schema", "api_spec", "config", "notebook"}
SUMMARY_BATCH_TOKENS = 6_000
MAX_FILES_TO_SUMMARISE = 60
EVIDENCE_TOKENS_PER_CRITERION = 6_000
MIN_EVIDENCE_CHUNKS = 3

INJECTION_RE = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|disregard the rubric|"
    r"(give|award|assign) (me |this project )?(full|maximum|100|10/10) (marks|points|score)|"
    r"you are now|system prompt|as an ai language model,? you must)",
    re.I,
)
ENTRYPOINT_NAMES = {"main", "app", "index", "server", "manage", "__main__", "api", "routes", "urls", "models"}


# ------------------------------------------------------------------ stage 1 helpers


def file_priority(path: str, category: str) -> float:
    """Heuristic relevance for summarisation order: entrypoints, README, schemas and
    shallow source files first; deep or generated-looking files last."""
    base = {"docs": 3, "api_spec": 3, "schema": 2.5, "code": 2, "config": 1.5, "test": 1.2, "notebook": 1.8}.get(category, 0)
    name = Path(path).stem.lower()
    if name.startswith("readme"):
        base += 5
    if name in ENTRYPOINT_NAMES:
        base += 2
    base -= 0.3 * path.count("/")
    if re.search(r"(generated|migrations/\d|\.pb\.|_pb2)", path):
        base -= 2
    return base


def build_chunks(root: Path, inv: Inventory, max_file_bytes: int) -> list[tuple[Chunk, str]]:
    out: list[tuple[Chunk, str]] = []
    for f in inv.files:
        if f.category not in TEXT_CATEGORIES or f.size > max_file_bytes:
            continue
        text = (root / f.path).read_text(encoding="utf-8", errors="ignore")
        if f.category == "notebook":
            text = _notebook_to_text(text)
        out.extend((c, f.category) for c in chunk_text(f.path, text))
    return out


def _notebook_to_text(raw: str) -> str:
    import json

    try:
        nb = json.loads(raw)
        return "\n\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))  # drop outputs/images
    except ValueError:
        return raw


def scan_prompt_injection(chunks: list[tuple[Chunk, str]]) -> list[str]:
    return sorted({c.ref for c, _ in chunks if INJECTION_RE.search(c.text)})


def render_tree(inv: Inventory, limit: int = 300) -> str:
    paths = sorted(f.path for f in inv.files)
    more = f"\n... and {len(paths) - limit} more files" if len(paths) > limit else ""
    return "\n".join(paths[:limit]) + more


def chunks_xml(chunks: list[Chunk]) -> str:
    return "\n".join(f'<chunk ref="{c.ref}">\n{c.text}\n</chunk>' for c in chunks)


# ------------------------------------------------------------------ stage 2


def understand(llm: LLMClient, inv: Inventory, chunks: list[tuple[Chunk, str]], assignment_brief: str) -> dict:
    # Representative chunk(s) per file: the first chunk(s) usually hold imports, the
    # module docstring and top-level definitions.
    first_by_file: dict[str, list[Chunk]] = {}
    for c, _ in chunks:
        lst = first_by_file.setdefault(c.path, [])
        if len(lst) < 2:
            lst.append(c)
    cat = {f.path: f.category for f in inv.files}
    ranked = sorted(first_by_file, key=lambda p: file_priority(p, cat.get(p, "other")), reverse=True)
    ranked = ranked[:MAX_FILES_TO_SUMMARISE]

    # Map: pack files into batches that fit the per-call budget.
    summaries: list[dict] = []
    batch: list[Chunk] = []
    used = 0
    for path in ranked + [None]:
        file_chunks = first_by_file.get(path, []) if path else []
        cost = sum(c.tokens for c in file_chunks)
        if batch and (path is None or used + cost > SUMMARY_BATCH_TOKENS):
            res = llm.structured(prompts.SYSTEM, prompts.file_summaries_prompt(chunks_xml(batch)), prompts.FileSummaries)
            summaries.extend(s.model_dump() for s in res.files)
            batch, used = [], 0
        if path:
            batch.extend(file_chunks)
            used += cost

    # Reduce: one overview of the whole project from the summaries + tree + facts.
    summary_text = "\n".join(f"- {s['path']} [{s['role']}]: {s['summary']}" for s in summaries)
    overview = llm.structured(
        prompts.SYSTEM,
        prompts.project_summary_prompt(inv.facts, render_tree(inv), summary_text, assignment_brief),
        prompts.ProjectSummary,
    )
    return {"file_summaries": summaries, "project": overview.model_dump()}


# ------------------------------------------------------------------ stage 3


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def verify_evidence(evidence: list[dict], offered: dict[str, Chunk]) -> list[dict]:
    """An LLM citation counts only if the quote really appears in a chunk of the
    cited file that was actually shown to the model. This catches hallucinated
    evidence deterministically.

    Models cite line-precise refs ("app/main.py:10") rather than copying the chunk
    range ("app/main.py:1-39") - seen in a live Gemini run - so we match on the
    file path and then record the real line number of the quote ourselves."""
    by_path: dict[str, list[Chunk]] = {}
    for c in offered.values():
        by_path.setdefault(c.path, []).append(c)
    out = []
    for ev in evidence:
        path = re.sub(r":[\d\-,\s]*$", "", ev.get("ref", "").strip())
        quote = _norm(ev.get("quote", ""))
        ok, ref = False, ev.get("ref", "")
        for c in by_path.get(path, []) if len(quote) >= 8 else []:  # tiny quotes prove nothing
            if quote in _norm(c.text):
                ok = True
                # first line of the chunk that the (possibly multi-line) quote starts on
                line = next((i for i, ln in enumerate(c.text.splitlines())
                             if _norm(ln) and (quote in _norm(ln) or quote.startswith(_norm(ln)))), 0)
                ref = f"{c.path}:{c.start_line + line}"
                break
        out.append({**ev, "ref": ref, "verified": ok})
    return out


def evaluate_criterion(llm: LLMClient, criterion: dict, ctx: dict, index: BM25Index, facts: dict) -> dict:
    query = " ".join([criterion["title"], criterion["description"], *(b["descriptor"] for b in criterion.get("scoring_guide", []))])
    cats = categories_for(criterion.get("evidence_sources", []))
    candidates = index.search(query, k=20, categories=cats)
    if len(candidates) < MIN_EVIDENCE_CHUNKS:
        # Lexical retrieval misses paraphrases ("features" vs "endpoints"); back-fill with
        # the most central files of the allowed categories (entrypoints, README...).
        seen = {c.ref for c in candidates}
        central = sorted((it for it in index.items if (cats is None or it[1] in cats) and it[0].ref not in seen),
                         key=lambda it: (-file_priority(it[0].path, it[1]), it[0].start_line))
        candidates += [c for c, _ in central[: MIN_EVIDENCE_CHUNKS * 2 - len(candidates)]]
    picked, used = [], 0
    for c in candidates:  # fill the evidence budget in relevance order
        if used + c.tokens > EVIDENCE_TOKENS_PER_CRITERION:
            continue
        picked.append(c)
        used += c.tokens
        if len(picked) >= 8:
            break
    project = ctx["project"]
    overview = f"Purpose: {project['purpose']}\nArchitecture: {project['architecture']}\nStack: {', '.join(project['tech_stack'])}"
    prompt = prompts.criterion_prompt(
        key=criterion["key"], title=criterion["title"], description=criterion["description"],
        max_score=criterion["max_score"], guide=criterion.get("scoring_guide", []),
        project_summary=overview, facts=facts, chunks_xml=chunks_xml(picked),
    )
    a = llm.structured(prompts.SYSTEM, prompt, prompts.CriterionAssessment)

    max_score = float(criterion["max_score"])
    score = min(max(a.score, 0.0), max_score)  # clamp: never trust the model's arithmetic bounds
    evidence = verify_evidence([e.model_dump() for e in a.evidence], {c.ref: c for c in picked})
    verified = sum(e["verified"] for e in evidence)
    confidence = a.confidence
    flags = []
    if evidence and verified < len(evidence):
        flags.append("unverified_evidence")
        confidence *= 0.5 + 0.5 * verified / len(evidence)
    if verified == 0 and score > 0.5 * max_score:
        # High marks with nothing verifiable to back them: cap and send to a human.
        flags.append("score_without_evidence")
        score = 0.5 * max_score
        confidence = min(confidence, 0.4)
    if not picked:
        flags.append("no_evidence_retrieved")
    return {
        "criterion_id": criterion["id"], "key": criterion["key"], "weight": criterion["weight"],
        "score": round(score, 2), "max_score": max_score, "confidence": round(confidence, 3),
        "rationale": a.rationale, "strengths": a.strengths, "improvements": a.improvements,
        "evidence": evidence, "flags": flags, "evidence_tokens": used,
    }


# ------------------------------------------------------------------ stage 4


@dataclass
class Aggregate:
    total: float           # 0-100
    confidence: float
    needs_review: bool
    reasons: list[str]


def aggregate(scores: list[dict], *, injection_refs: list[str], facts: dict, threshold: float,
              pass_mark: float = 50.0, borderline: float = 5.0) -> Aggregate:
    total = 100 * sum(s["weight"] * s["score"] / s["max_score"] for s in scores)
    conf = min((s["confidence"] for s in scores), default=0.0)
    reasons = []
    if conf < threshold:
        reasons.append(f"low_confidence:{conf:.2f}")
    if any("score_without_evidence" in s["flags"] for s in scores):
        reasons.append("score_without_evidence")
    if injection_refs:
        reasons.append("prompt_injection_suspected")
    if abs(total - pass_mark) <= borderline:
        reasons.append("borderline_pass_fail")
    if facts.get("possible_committed_secrets"):
        reasons.append("committed_secrets")  # not a scoring issue, but a mentor should tell the student
    return Aggregate(round(total, 2), round(conf, 3), bool(reasons), reasons)


def estimate_project_tokens(chunks: list[tuple[Chunk, str]]) -> int:
    return sum(c.tokens for c, _ in chunks)


__all__ = ["understand", "evaluate_criterion", "aggregate", "build_chunks", "scan_prompt_injection",
           "estimate_tokens", "estimate_project_tokens"]
