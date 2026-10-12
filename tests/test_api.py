from app.worker import process_one


def test_start_evaluation_five_times_creates_one_job(env, submit):
    sub = submit()
    c, h = env["client"], env["h"]["student"]
    codes, jobs = [], set()
    for _ in range(5):
        r = c.post(f"/v1/submissions/{sub['id']}/evaluations", headers=h)
        codes.append(r.status_code)
        jobs.add(r.json()["job_id"])
    assert codes == [202, 200, 200, 200, 200]
    assert len(jobs) == 1


def test_idempotency_key_replay_and_mismatch(env):
    c, h = env["client"], env["h"]["student"]
    body = {"assignment_id": env["ids"]["assignment_id"], "artifacts": [{"kind": "zip", "filename": "a.zip"}]}
    r1 = c.post("/v1/submissions", headers={**h, "Idempotency-Key": "k1"}, json=body)
    r2 = c.post("/v1/submissions", headers={**h, "Idempotency-Key": "k1"}, json=body)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers["Idempotent-Replayed"] == "true"
    r3 = c.post("/v1/submissions", headers={**h, "Idempotency-Key": "k1"}, json={**body, "notes": "changed"})
    assert r3.status_code == 422


def test_students_cannot_see_each_other(env, submit):
    sub = submit()
    c = env["client"]
    assert c.get(f"/v1/submissions/{sub['id']}", headers=env["h"]["student2"]).status_code == 404
    assert c.get(f"/v1/submissions/{sub['id']}", headers=env["h"]["evaluator"]).status_code == 200
    assert c.get(f"/v1/submissions/{sub['id']}").status_code == 401
    assert c.get(f"/v1/submissions/{sub['id']}", headers={"Authorization": "Bearer x.y.z"}).status_code == 401


def test_role_checks(env):
    c = env["client"]
    rubric = {"criteria": [{"key": "only", "title": "Only", "description": "d", "weight": 1.0}]}
    url = f"/v1/assignments/{env['ids']['assignment_id']}/rubric"
    assert c.put(url, headers=env["h"]["student"], json=rubric).status_code == 403
    assert c.get("/v1/reviews", headers=env["h"]["student"]).status_code == 403


def test_upload_validation(env):
    c, h = env["client"], env["h"]["student"]
    r = c.post("/v1/submissions", headers=h, json={
        "assignment_id": env["ids"]["assignment_id"], "artifacts": [{"kind": "zip", "filename": "a.zip"}]})
    url = r.json()["artifacts"][0]["upload_url"]
    assert c.post(url, headers=h, files={"file": ("a.zip", b"MZ\x90\x00not-a-zip")}).status_code == 415


def test_link_artifacts_validated(env):
    c, h = env["client"], env["h"]["student"]
    for art in [{"kind": "github", "source_url": "https://evil.com/a/b"},
                {"kind": "live_url", "source_url": "http://169.254.169.254/latest"},
                {"kind": "zip", "filename": "../../x.zip"}]:
        r = c.post("/v1/submissions", headers=h, json={"assignment_id": env["ids"]["assignment_id"], "artifacts": [art]})
        assert r.status_code == 422, art


def test_rubric_versioning(env):
    c, h = env["client"], env["h"]["instructor"]
    url = f"/v1/assignments/{env['ids']['assignment_id']}/rubric"
    bad = {"criteria": [{"key": "a", "title": "A", "description": "d", "weight": 0.5}]}
    assert c.put(url, headers=h, json=bad).status_code == 422  # weights must sum to 1
    good = {"criteria": [
        {"key": "code", "title": "Code", "description": "d", "weight": 0.6, "evidence_sources": ["code"],
         "scoring_guide": [{"min": 0, "max": 10, "descriptor": "x"}]},
        {"key": "docs", "title": "Docs", "description": "d", "weight": 0.4, "evidence_sources": ["docs"]}]}
    r = c.put(url, headers=h, json=good)
    assert r.status_code == 200 and r.json()["version"] == 2
    cur = c.get(url, headers=env["h"]["student"]).json()
    assert cur["version"] == 2 and [x["key"] for x in cur["criteria"]] == ["code", "docs"]


def test_full_flow_with_review_gate(env, submit):
    sub = submit()
    c, hs, he = env["client"], env["h"]["student"], env["h"]["evaluator"]
    job = c.post(f"/v1/submissions/{sub['id']}/evaluations", headers=hs).json()
    assert c.get(f"/v1/evaluations/{job['job_id']}/result", headers=hs).status_code == 409  # not done yet
    process_one("w1", storage=env["storage"])
    st = c.get(f"/v1/evaluations/{job['job_id']}", headers=hs).json()
    assert st["status"] == "succeeded" and st["progress"] == 100
    staff = c.get(f"/v1/evaluations/{job['job_id']}/result", headers=he).json()
    assert len(staff["criteria"]) == 6 and 0 <= staff["total_score"] <= 100
    assert all(e["verified"] for cr in staff["criteria"] for e in cr["evidence"])
    student_view = c.get(f"/v1/evaluations/{job['job_id']}/result", headers=hs)
    if staff["status"] == "approved":
        assert student_view.status_code == 200 and student_view.json()["flags"] is None
    else:
        assert student_view.status_code == 409

    # student disputes once it's released, evaluator overrides one criterion
    if staff["status"] != "approved":
        rev = c.get("/v1/reviews", headers=he).json()[0]
        d = c.post(f"/v1/reviews/{rev['review_id']}/decision", headers=he, json={"decision": "approve", "comment": "looks right"})
        assert d.status_code == 200, d.text
    r = c.post(f"/v1/results/{staff['result_id']}/reviews", headers=hs, json={"reason": "My docs deserve more credit."})
    assert r.status_code == 201, r.text
    assert c.post(f"/v1/results/{staff['result_id']}/reviews", headers=hs,
                  json={"reason": "again please, pretty please"}).status_code in (200, 409)
    rid = r.json()["review_id"]
    assert c.post(f"/v1/reviews/{rid}/claim", headers=he).status_code == 200
    assert c.post(f"/v1/reviews/{rid}/claim", headers=he).status_code == 409
    d = c.post(f"/v1/reviews/{rid}/decision", headers=he, json={
        "decision": "approve", "comment": "agreed", "overrides": {"documentation": {"score": 10}}}).json()
    assert d["overrides"]["documentation"]["to"] == 10
    assert c.post(f"/v1/reviews/{rid}/decision", headers=he,
                  json={"decision": "reject", "comment": "late"}).status_code == 409


def test_reject_with_re_evaluation_creates_new_job(env, submit):
    sub = submit()
    c, hs, he = env["client"], env["h"]["student"], env["h"]["evaluator"]
    job = c.post(f"/v1/submissions/{sub['id']}/evaluations", headers=hs).json()
    process_one("w1", storage=env["storage"])
    res = c.get(f"/v1/evaluations/{job['job_id']}/result", headers=he).json()
    r = c.post(f"/v1/results/{res['result_id']}/reviews", headers=he, json={"reason": "spot check by staff"})
    rid = (r.json() if r.status_code in (200, 201) else None)["review_id"]
    d = c.post(f"/v1/reviews/{rid}/decision", headers=he,
               json={"decision": "reject", "comment": "scores unreliable", "re_evaluate": True}).json()
    assert d["result_status"] == "rejected" and d["re_evaluation_job_id"]
    assert process_one("w1", storage=env["storage"]) == d["re_evaluation_job_id"]
    history = c.get(f"/v1/submissions/{sub['id']}/results", headers=he).json()
    assert [h["status"] for h in history][0] == "rejected" and len(history) == 2
