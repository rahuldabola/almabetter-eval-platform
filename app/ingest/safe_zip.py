"""Hardened ZIP extraction for untrusted student uploads.

Defends against:
  * Zip Slip / path traversal  (../../etc/passwd, absolute paths, drive letters)
  * Symlink entries            (a link pointing outside the sandbox)
  * Zip bombs                  (total uncompressed size, per-entry ratio, file count)
  * Lying headers              (we count bytes actually written, not the declared size)
  * Encrypted entries          (rejected; we cannot evaluate them anyway)
"""
from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CHUNK = 1024 * 64


class UnsafeArchive(Exception):
    """Raised for archives we refuse to process. `code` is stored on the job/artifact."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ExtractionReport:
    files: int
    bytes_written: int
    skipped: list[str]


def _safe_member_path(name: str, dest: Path) -> Path | None:
    # Normalise Windows separators, refuse absolute paths and drive letters.
    norm = name.replace("\\", "/")
    p = PurePosixPath(norm)
    if p.is_absolute() or (p.parts and ":" in p.parts[0]):
        raise UnsafeArchive("path_traversal", f"absolute path in archive: {name!r}")
    if any(part == ".." for part in p.parts):
        raise UnsafeArchive("path_traversal", f"parent reference in archive: {name!r}")
    target = (dest / Path(*p.parts)).resolve() if p.parts else None
    if target is None:
        return None
    if not target.is_relative_to(dest.resolve()):
        raise UnsafeArchive("path_traversal", f"entry escapes extraction dir: {name!r}")
    return target


def safe_extract(
    archive: Path,
    dest: Path,
    *,
    max_total_bytes: int,
    max_files: int,
    max_ratio: int,
) -> ExtractionReport:
    if not zipfile.is_zipfile(archive):
        raise UnsafeArchive("corrupt_archive", "file is not a valid ZIP archive")
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    count = 0
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > max_files:
                raise UnsafeArchive("too_many_files", f"{len(infos)} entries > limit {max_files}")
            declared = sum(i.file_size for i in infos)
            if declared > max_total_bytes:
                raise UnsafeArchive("archive_too_large", f"declared size {declared} > limit {max_total_bytes}")
            for info in infos:
                if info.flag_bits & 0x1:
                    raise UnsafeArchive("encrypted_archive", "encrypted entries are not supported")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    skipped.append(info.filename)  # never materialise symlinks
                    continue
                target = _safe_member_path(info.filename, dest)
                if target is None:
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if info.compress_size and info.file_size / info.compress_size > max_ratio:
                    raise UnsafeArchive("zip_bomb", f"compression ratio too high for {info.filename!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                # Stream and count real bytes: headers can lie about file_size.
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        buf = src.read(CHUNK)
                        if not buf:
                            break
                        written += len(buf)
                        if written > max_total_bytes:
                            raise UnsafeArchive("zip_bomb", "extracted bytes exceeded limit")
                        out.write(buf)
                count += 1
    except zipfile.BadZipFile as e:
        raise UnsafeArchive("corrupt_archive", str(e)) from e
    return ExtractionReport(files=count, bytes_written=written, skipped=skipped)
