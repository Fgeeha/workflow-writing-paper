import yaml
from fastapi.testclient import TestClient

from workflow_writing_paper.core import Workspace
from workflow_writing_paper.eval import coverage, run_bench, summary
from workflow_writing_paper.web import create_app

FAKE_POOL = {
    "providers": [
        {"id": "cand-a", "kind": "fake", "family": "a", "reply": "Нет сравнения с базовой линией, малая выборка."},
        {"id": "cand-b", "kind": "fake", "family": "b", "reply": "Замечаний к тексту нет."},
        {"id": "cand-c", "kind": "fake", "family": "c", "reply": "Текст написан хорошо."},
        {"id": "cand-en", "kind": "fake", "family": "e", "reply": "No baseline, small sample."},
        {"id": "judge-a", "kind": "fake", "family": "a", "score": 9},
        {"id": "judge-x", "kind": "fake", "family": "x", "score": 6},
        {"id": "judge-y", "kind": "fake", "family": "y", "score": 8},
    ],
    "roles": {"critic": "cand-a", "judge": ["judge-a", "judge-x", "judge-y"]},
    "profiles": {"p": {"critic": "cand-b"}},
}


def make_ws(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.init()
    (ws.root / "providers.yaml").write_text(yaml.safe_dump(FAKE_POOL, allow_unicode=True), encoding="utf-8")
    return ws


def test_coverage_stems():
    expected = {"must_mention": ["сравнение с базовой линией", "малая выборка", "обобщение"]}
    assert coverage("Нет сравнения с базовой линией, выборка малая.", expected) == 2 / 3
    assert coverage("что угодно", {}) is None


def test_bench_three_models_table(tmp_path):
    ws = make_ws(tmp_path)
    run_bench(ws, "critic", ["cand-a", "cand-b", "cand-c"])
    db = ws.db()
    table, rec = summary(db, "critic")
    rows = db.execute("SELECT * FROM bench_runs").fetchall()
    assert len(rows) == 9 and all(r["error"] is None for r in rows)
    by = {t["provider"]: t for t in table}
    assert set(by) == {"cand-a", "cand-b", "cand-c"}
    # cand-a (семейство a) не судит judge-a: судьи x(6) и y(8) → медиана 7; остальных судят a(9) и x(6) → 7.5
    assert by["cand-a"]["judge"] == 7.0 and by["cand-b"]["judge"] == 7.5
    assert by["cand-a"]["coverage"] > by["cand-b"]["coverage"]
    assert rec.startswith("cand-b")  # при равенстве судей берётся первый по стоимости/имени
    # оценка автора перекрывает судей
    db.execute("UPDATE bench_runs SET human_score = 5 WHERE provider = 'cand-c'")
    db.commit()
    assert summary(db, "critic")[1].startswith("cand-c")
    assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 9 * 3


def test_bench_not_enough_foreign_judges(tmp_path):
    ws = make_ws(tmp_path)
    cfg = ws.config()
    cfg["roles"]["judge"] = ["judge-a"]
    ws.save_config(cfg)
    run_bench(ws, "critic", ["cand-a"])
    rows = ws.db().execute("SELECT error, judge_overall FROM bench_runs").fetchall()
    assert all("0 из 2" in r["error"] and r["judge_overall"] is None for r in rows)


def test_web_pages(tmp_path):
    ws = make_ws(tmp_path)
    run_bench(ws, "critic", ["cand-b"])
    client = TestClient(create_app(ws))
    assert "cand-a" in client.get("/pool").text
    assert "доступен" in client.get("/pool/ping/cand-a").text
    assert client.post("/pool/profile/p", follow_redirects=False).status_code == 303
    assert ws.config()["roles"]["critic"] == "cand-b"
    resp = client.post("/pool/roles", data={"role:critic": "cand-c", "role:judge": "judge-x, judge-y"},
                       follow_redirects=False)
    assert resp.status_code == 303 and ws.config()["roles"]["judge"] == ["judge-x", "judge-y"]
    assert client.post("/pool/roles", data={"role:critic": "nope"}).status_code == 400
    page = client.get("/bench?role=critic").text
    assert "cand-b" in page and "Рекомендация" in page
    rid = ws.db().execute("SELECT id FROM bench_runs LIMIT 1").fetchone()[0]
    assert client.post(f"/bench/score/{rid}", data={"score": "4"}).text == "<b>4</b>"
    assert client.get(f"/bench/answer/{rid}").status_code == 200
    assert "judge" in client.get("/calls").text
    assert client.get("/calls/1").status_code == 200


def test_rubric_weights_and_overall():
    from workflow_writing_paper.core.workspace import DEFAULTS
    from workflow_writing_paper.eval.bench import rubric_weights, weighted_overall

    w = rubric_weights((DEFAULTS / "rubrics" / "critic.md").read_text(encoding="utf-8"))
    assert w == {"C1": 0.4, "C2": 0.3, "C3": 0.2, "C4": 0.1}
    assert weighted_overall({"C1": 10, "C2": 5, "C3": 0, "C4": 10}, w) == 6.5
    assert weighted_overall({"C1": 10, "C2": 5, "C3": 0}, w) is None  # нет критерия
    assert weighted_overall({"C1": 70, "C2": 5, "C3": 0, "C4": 1}, w) is None  # вне шкалы


def test_language_check_marks_foreign_answer(tmp_path):
    from workflow_writing_paper.eval.bench import language_share

    assert language_share("Нет сравнения с базовой линией (baseline) и мала выборка.", "ru") > 0.8
    assert language_share("No baseline at all", "ru") == 0.0
    assert language_share("12 + 3 = 15", "ru") == 1.0
    ws = make_ws(tmp_path)
    run_bench(ws, "critic", ["cand-en"])
    rows = ws.db().execute("SELECT error FROM bench_runs").fetchall()
    assert all("не на языке задачи" in r["error"] for r in rows)


def test_decision_judge_scores_via_systemone(tmp_path, monkeypatch):
    from workflow_writing_paper.providers import decision

    def fake_request(url, body=None, headers=None, timeout=0, context=None):
        assert url.endswith("/v1/systemone") and set(body["questions"]) == {"C1", "C2", "C3", "C4"}
        assert body["questions"]["C1"]["type"] == "score"
        return {"answers": {c: {"type": "score", "score": 3.0, "probabilities": {"3": 1.0}} for c in body["questions"]},
                "usage": {"input_tokens": 100}}

    monkeypatch.setattr(decision, "_request", fake_request)
    ws = make_ws(tmp_path)
    cfg = ws.config()
    cfg["providers"].append({"id": "dec", "kind": "ollama-decision", "family": "d", "model": "kev"})
    cfg["roles"]["judge"] = ["dec", "judge-x"]
    ws.save_config(cfg)
    run_bench(ws, "critic", ["cand-a"])
    import json
    row = ws.db().execute("SELECT judge_scores, judge_overall, error FROM bench_runs").fetchone()
    js = json.loads(row["judge_scores"])
    assert js["dec"]["scores"] == {"C1": 7.5, "C2": 7.5, "C3": 7.5, "C4": 7.5} and js["dec"]["overall"] == 7.5
    assert row["error"] is None and row["judge_overall"] == 6.75  # медиана (7.5, 6)
    assert ws.db().execute("SELECT count(*) FROM calls WHERE provider='dec'").fetchone()[0] == 3


def test_rubric_criteria_texts():
    from workflow_writing_paper.core.workspace import DEFAULTS
    from workflow_writing_paper.eval.bench import rubric_criteria

    c = rubric_criteria((DEFAULTS / "rubrics" / "critic.md").read_text(encoding="utf-8"))
    assert set(c) == {"C1", "C2", "C3", "C4"} and c["C1"].startswith("Находит существенные")
