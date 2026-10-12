"""Object storage abstraction. Local filesystem in the PoC; S3 in production
(same key layout: submissions/<submission_id>/<artifact_id>/<filename>)."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO

from app.config import settings


class LocalStorage:
    def __init__(self, root: Path | None = None):
        self.root = (root or settings.storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("invalid storage key")
        return p

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> tuple[int, str]:
        """Stream to storage while hashing and enforcing the size cap."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        n = 0
        with open(target, "wb") as out:
            while chunk := stream.read(1024 * 1024):
                n += len(chunk)
                if n > max_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise ValueError(f"upload exceeds {max_bytes} bytes")
                h.update(chunk)
                out.write(chunk)
        return n, h.hexdigest()

    def workdir(self, job_id: str) -> Path:
        p = self.path(f"work/{job_id}")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def cleanup_workdir(self, job_id: str) -> None:
        shutil.rmtree(self.path(f"work/{job_id}"), ignore_errors=True)
