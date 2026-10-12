"""Structure-aware chunking.

Code is split on top-level definition boundaries (class/def/function/export) so
a chunk is a meaningful unit; prose is split on headings/paragraphs. Every chunk
carries its file path and line range so the LLM can cite evidence that we can
verify afterwards. Production would use tree-sitter for exact syntax-tree
boundaries across languages; the regex heuristic keeps the PoC dependency-free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ~4 chars per token is the usual rule of thumb for English/code with BPE tokenizers.
CHARS_PER_TOKEN = 4

BOUNDARY_RE = re.compile(
    r"^(?:class |def |async def |func |fn |public |private |protected |export |function |const \w+ = \(|@app\.|@router\.|#{1,3} )"
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class Chunk:
    path: str
    start_line: int
    end_line: int
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def ref(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


def chunk_text(path: str, text: str, max_tokens: int = 800, overlap_lines: int = 3) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    max_chars = max_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    start = 0
    size = 0
    for i, line in enumerate(lines):
        at_boundary = i > start and BOUNDARY_RE.match(line) is not None
        if size + len(line) > max_chars and i > start or (at_boundary and size > max_chars // 3):
            chunks.append(Chunk(path, start + 1, i, "\n".join(lines[start:i])))
            start = max(start + 1, i - overlap_lines) if not at_boundary else i
            size = sum(len(x) + 1 for x in lines[start:i])
        size += len(line) + 1
    chunks.append(Chunk(path, start + 1, len(lines), "\n".join(lines[start:])))
    return [c for c in chunks if c.text.strip()]
