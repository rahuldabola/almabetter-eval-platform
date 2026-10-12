"""Runtime settings. Everything comes from the environment so the same image runs
in dev, CI and production (12-factor). Secrets are injected by the platform
(AWS Secrets Manager -> ECS task env) and never baked into the image."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./evalplatform.db"))
    # Local stand-in for S3. In production this is an S3 bucket + prefix.
    storage_dir: Path = field(default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./.storage")))

    # LLM
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "fake"))  # fake | gemini
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-latest"))
    llm_timeout_s: int = field(default_factory=lambda: _int("LLM_TIMEOUT_S", 60))
    llm_max_retries: int = field(default_factory=lambda: _int("LLM_MAX_RETRIES", 4))
    # Hard per-job token budget: protects cost and stops runaway loops.
    job_token_budget: int = field(default_factory=lambda: _int("JOB_TOKEN_BUDGET", 400_000))

    # Queue / worker
    lease_seconds: int = field(default_factory=lambda: _int("LEASE_SECONDS", 120))
    job_max_attempts: int = field(default_factory=lambda: _int("JOB_MAX_ATTEMPTS", 3))

    # Ingestion limits (defence against zip bombs / huge repos)
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 2 * 1024**3))
    max_extracted_bytes: int = field(default_factory=lambda: _int("MAX_EXTRACTED_BYTES", 3 * 1024**3))
    max_files: int = field(default_factory=lambda: _int("MAX_FILES", 50_000))
    max_compression_ratio: int = field(default_factory=lambda: _int("MAX_COMPRESSION_RATIO", 100))
    max_file_bytes_for_analysis: int = field(default_factory=lambda: _int("MAX_FILE_BYTES_FOR_ANALYSIS", 512 * 1024))

    # Review policy
    review_confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv("REVIEW_CONFIDENCE_THRESHOLD", "0.6"))
    )


settings = Settings()
