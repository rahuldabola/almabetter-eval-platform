"""Deterministic project inventory: walk the extracted tree, drop noise, classify
what is left, and compute objective facts the LLM should not have to guess
(languages, test count, presence of README/Dockerfile/CI, committed secrets...).

Nothing here calls an LLM - it is cheap, reproducible and runs first.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORED_DIRS = {
    ".git", "node_modules", "bower_components", "vendor", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", "out",
    "target", ".next", ".nuxt", ".gradle", ".idea", ".vscode", "coverage", ".terraform",
    "Pods", "DerivedData", ".ipynb_checkpoints", "site-packages",
}
IGNORED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "composer.lock", "Cargo.lock", "go.sum", ".DS_Store", "Thumbs.db",
}
CODE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".cpp": "C++", ".c": "C", ".h": "C", ".swift": "Swift", ".scala": "Scala",
    ".dart": "Dart", ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    ".sh": "Shell", ".ipynb": "Notebook", ".r": "R",
}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
SCHEMA_EXT = {".sql", ".prisma", ".graphql", ".gql"}
CONFIG_NAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "makefile",
    "requirements.txt", "pyproject.toml", "package.json", "pom.xml", "build.gradle", "go.mod",
    "cargo.toml", "gemfile", ".env.example", "tsconfig.json", "nginx.conf", "procfile",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}
PDF_EXT = {".pdf"}
DATA_EXT = {".csv", ".parquet", ".xlsx", ".pkl", ".h5", ".npy", ".db", ".sqlite"}
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_[^/]+|[^/]+_test\.\w+|[^/]+\.(test|spec)\.\w+)$")
API_SPEC_RE = re.compile(r"(openapi|swagger)[^/]*\.(ya?ml|json)$", re.I)
SECRET_FILE_RE = re.compile(r"(^|/)(\.env(\.\w+)?|id_rsa|.*\.pem|credentials\.json|service[-_]account.*\.json)$")
SECRET_CONTENT_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35}|ghp_[A-Za-z0-9]{36}|-----BEGIN (RSA |EC )?PRIVATE KEY-----)"
)


@dataclass
class FileInfo:
    path: str
    category: str     # code|test|docs|schema|api_spec|config|notebook|pdf|image|video|data|other
    language: str | None
    size: int
    lines: int = 0


@dataclass
class Inventory:
    files: list[FileInfo] = field(default_factory=list)
    ignored: Counter = field(default_factory=Counter)
    facts: dict = field(default_factory=dict)

    def by_category(self, *cats: str) -> list[FileInfo]:
        return [f for f in self.files if f.category in cats]

    def to_dict(self) -> dict:
        return {"files": [asdict(f) for f in self.files], "ignored": dict(self.ignored), "facts": self.facts}


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return True


def _looks_minified(path: Path) -> bool:
    if path.suffix not in {".js", ".css"}:
        return False
    if ".min." in path.name:
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)
        return head.count("\n") < 2 and len(head) >= 4000
    except OSError:
        return False


def classify(rel: str, path: Path) -> tuple[str, str | None]:
    name = path.name.lower()
    ext = path.suffix.lower()
    if TEST_RE.search(rel.lower()) and ext in CODE_EXT:
        return "test", CODE_EXT[ext]
    if API_SPEC_RE.search(name):
        return "api_spec", None
    if name in CONFIG_NAMES or name.startswith("dockerfile") or rel.startswith(".github/workflows/"):
        return "config", None
    if ext == ".ipynb":
        return "notebook", "Notebook"
    if ext in CODE_EXT:
        return "code", CODE_EXT[ext]
    if ext in DOC_EXT or name.startswith("readme"):
        return "docs", None
    if ext in SCHEMA_EXT or "migration" in rel.lower():
        return "schema", None
    if ext in PDF_EXT:
        return "pdf", None
    if ext in IMAGE_EXT:
        return "image", None
    if ext in VIDEO_EXT:
        return "video", None
    if ext in DATA_EXT:
        return "data", None
    if ext in {".yml", ".yaml", ".toml", ".ini", ".cfg", ".json"}:
        return "config", None
    return "other", None


def build_inventory(root: Path, max_file_bytes: int) -> Inventory:
    inv = Inventory()
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for d in dirnames:
            if d in IGNORED_DIRS or d.startswith("."):
                if d != ".github":
                    inv.ignored[f"dir:{d}"] += 1
                    continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                inv.ignored["symlink"] += 1
                continue
            if fn in IGNORED_FILES:
                inv.ignored["lockfile"] += 1
                continue
            size = p.stat().st_size
            category, lang = classify(rel, p)
            textual = category in {"code", "test", "docs", "schema", "api_spec", "config", "notebook"}
            if textual and _is_binary(p):
                inv.ignored["binary"] += 1
                continue
            if textual and _looks_minified(p):
                inv.ignored["minified"] += 1
                continue
            lines = 0
            if textual and size <= max_file_bytes:
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = sum(1 for _ in fh)
            elif textual:
                inv.ignored["too_large_for_analysis"] += 1
            inv.files.append(FileInfo(rel, category, lang, size, lines))
    inv.facts = compute_facts(root, inv)
    return inv


def compute_facts(root: Path, inv: Inventory) -> dict:
    loc = Counter()
    for f in inv.files:
        if f.language and f.category in {"code", "test"}:
            loc[f.language] += f.lines
    paths = [f.path for f in inv.files]
    lower = [p.lower() for p in paths]
    secrets = [p for p in paths if SECRET_FILE_RE.search(p) and not p.endswith(".example")]
    for f in inv.files:
        if f.category in {"code", "config"} and f.size < 200_000:
            txt = (root / f.path).read_text(encoding="utf-8", errors="ignore")
            if SECRET_CONTENT_RE.search(txt):
                secrets.append(f.path)
    return {
        "total_files_analysed": len(inv.files),
        "lines_of_code_by_language": dict(loc.most_common()),
        "primary_language": loc.most_common(1)[0][0] if loc else None,
        "test_files": sum(1 for f in inv.files if f.category == "test"),
        "has_readme": any(p.split("/")[-1].startswith("readme") for p in lower),
        "has_dockerfile": any(p.split("/")[-1].startswith("dockerfile") for p in lower),
        "has_ci": any(p.startswith(".github/workflows/") or p == ".gitlab-ci.yml" for p in lower),
        "has_api_spec": any(f.category == "api_spec" for f in inv.files),
        "has_db_schema": any(f.category == "schema" for f in inv.files),
        "docs_files": sum(1 for f in inv.files if f.category == "docs"),
        "pdfs": sum(1 for f in inv.files if f.category == "pdf"),
        "images": sum(1 for f in inv.files if f.category == "image"),
        "videos": sum(1 for f in inv.files if f.category == "video"),
        "possible_committed_secrets": sorted(set(secrets)),
    }
