import io
import stat
import zipfile

import pytest

from app.ingest.chunker import chunk_text
from app.ingest.classify import build_inventory
from app.ingest.safe_zip import UnsafeArchive, safe_extract
from app.ingest.sources import InvalidSource, parse_github_url, validate_live_url
from tests.conftest import make_zip

LIMITS = dict(max_total_bytes=10_000_000, max_files=100, max_ratio=100)


def _write(tmp_path, data: bytes):
    p = tmp_path / "a.zip"
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("name", ["../evil.txt", "a/../../evil.txt", "/etc/passwd", "C:/Windows/x.txt", "..\\evil.txt"])
def test_zip_slip_rejected(tmp_path, name):
    with pytest.raises(UnsafeArchive) as e:
        safe_extract(_write(tmp_path, make_zip({name: "x"})), tmp_path / "out", **LIMITS)
    assert e.value.code == "path_traversal"
    assert not (tmp_path / "evil.txt").exists()


def test_zip_bomb_ratio_rejected(tmp_path):
    data = make_zip({"bomb.txt": b"\0" * 5_000_000})  # compresses ~1000:1
    with pytest.raises(UnsafeArchive) as e:
        safe_extract(_write(tmp_path, data), tmp_path / "out", **LIMITS)
    assert e.value.code == "zip_bomb"


def test_total_size_and_file_count_limits(tmp_path):
    with pytest.raises(UnsafeArchive) as e:
        safe_extract(_write(tmp_path, make_zip({f"f{i}.txt": "x" for i in range(101)})), tmp_path / "o", **LIMITS)
    assert e.value.code == "too_many_files"
    with pytest.raises(UnsafeArchive) as e:
        safe_extract(_write(tmp_path, make_zip({"big.bin": bytes(range(256)) * 50_000})), tmp_path / "o2",
                     max_total_bytes=1_000_000, max_files=10, max_ratio=100)
    assert e.value.code == "archive_too_large"


def test_symlink_entries_skipped(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("ok.txt", "fine")
    rep = safe_extract(_write(tmp_path, buf.getvalue()), tmp_path / "out", **LIMITS)
    assert rep.skipped == ["link"] and rep.files == 1
    assert not (tmp_path / "out" / "link").exists()


def test_corrupt_zip(tmp_path):
    with pytest.raises(UnsafeArchive) as e:
        safe_extract(_write(tmp_path, b"PK\x03\x04garbage"), tmp_path / "out", **LIMITS)
    assert e.value.code == "corrupt_archive"


def test_github_url_parsing():
    r = parse_github_url("https://github.com/octo-org/my.repo.git")
    assert (r.owner, r.repo) == ("octo-org", "my.repo")
    for bad in ["http://github.com/a/b", "https://github.com.evil.com/a/b", "https://github.com/a/b/../../x",
                "https://github.com/a", "git@github.com:a/b.git", "https://gitlab.com/a/b"]:
        with pytest.raises(InvalidSource):
            parse_github_url(bad)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/",
    "http://localhost:8080/", "http://[::1]/", "ftp://example.com/x", "http://user:pw@example.com/",
    "http://example.com:22/", "http://metadata.internal/",
])
def test_ssrf_blocked(url):
    with pytest.raises(InvalidSource):
        validate_live_url(url)


def test_inventory_ignores_noise_and_finds_facts(tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.js").write_text("x")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n")
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "README.md").write_text("# Hi\n")
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\0\0")
    inv = build_inventory(tmp_path, 512 * 1024)
    paths = {f.path for f in inv.files}
    assert "node_modules/x/i.js" not in paths and "package-lock.json" not in paths
    assert inv.facts["test_files"] == 1 and inv.facts["has_readme"]
    assert inv.facts["primary_language"] == "Python"
    assert ".env" in inv.facts["possible_committed_secrets"]


def test_chunks_cover_file_with_line_refs():
    text = "\n".join(f"def f{i}():\n    return {i}\n" for i in range(400))
    chunks = chunk_text("m.py", text, max_tokens=200)
    assert len(chunks) > 3
    assert chunks[0].start_line == 1 and chunks[-1].end_line == len(text.splitlines())
    assert all(c.tokens <= 260 for c in chunks)
    assert all(c.text.splitlines()[0] == text.splitlines()[c.start_line - 1] for c in chunks)
