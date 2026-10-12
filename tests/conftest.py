import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api, auth, db
from app.seed import seed
from app.storage import LocalStorage

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "task-manager-api"


def make_zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def zip_dir(src: Path, extra: dict[str, str] | None = None) -> bytes:
    files = {p.relative_to(src).as_posix(): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    files.update(extra or {})
    return make_zip(files)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pg = os.getenv("TEST_DATABASE_URL")
    db.configure(pg or f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    if pg:
        db.Base.metadata.drop_all(db.engine)
    db.init_db()
    storage = LocalStorage(tmp_path / "storage")
    monkeypatch.setattr(api, "storage", storage)
    monkeypatch.setattr("app.llm.client._sleep", lambda _: None)  # no real backoff waits in tests
    for lim in (auth.write_limiter, auth.evaluation_limiter):
        lim.hits.clear()
    with db.SessionLocal() as s:
        ids = seed(s)
    client = TestClient(api.app)
    hdr = {k: {"Authorization": f"Bearer {v}"} for k, v in ids["tokens"].items()}
    return {"ids": ids, "client": client, "h": hdr, "storage": storage}


@pytest.fixture()
def submit(env):
    """Create a submission with one uploaded ZIP; returns submission JSON."""

    def _submit(zip_bytes: bytes | None = None, who: str = "student"):
        c, h = env["client"], env["h"][who]
        r = c.post("/v1/submissions", headers=h, json={
            "assignment_id": env["ids"]["assignment_id"],
            "artifacts": [{"kind": "zip", "filename": "p.zip"}],
        })
        assert r.status_code == 201, r.text
        sub = r.json()
        data = zip_bytes if zip_bytes is not None else zip_dir(SAMPLE)
        r = c.post(sub["artifacts"][0]["upload_url"], headers=h, files={"file": ("p.zip", data)})
        assert r.status_code == 200, r.text
        return r.json()

    return _submit
