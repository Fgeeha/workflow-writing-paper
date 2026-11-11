"""Живое A/B: стадия двумя моделями, выбор автора → ab_choices → доля побед в таблице стенда."""

from contextlib import closing

from fastapi.testclient import TestClient
from test_pipeline import NOVELTY_OK, POOL, make

from workflow_writing_paper.cli import main
from workflow_writing_paper.core.engine import enqueue, run_task
from workflow_writing_paper.eval.bench import summary
from workflow_writing_paper.web import create_app

AB_POOL = {**POOL, "providers": [*POOL["providers"],
                                 {"id": "writer-b", "kind": "fake", "family": "c", "reply": NOVELTY_OK + "\nВариант B.\n"},
                                 {"id": "writer-dead", "kind": "fake", "family": "c", "reply": "  "}]}  # пустой ответ


def test_ab_novelty_choose_a_then_b(tmp_path):
    ws, project = make(tmp_path, AB_POOL)
    res = run_task(ws, enqueue(ws, "demo", "novelty", {"input": "Мы ускорили инференс.", "ab": "writer-b"}))
    assert res["error"] is None and res["ab"]["a"] == {"provider": "writer", "version": 1}
    assert res["ab"]["b"] == {"provider": "writer-b", "version": 2, "error": None}
    project = type(project).open(ws, "demo")
    assert "roles" not in project.manifest or "ideator" not in project.manifest["roles"]  # подмена только в памяти
    cur = project.artifact("novelty")
    assert "Вариант B" in cur.body and cur.meta["created_by"]["provider"] == "writer-b" and cur.meta["reviews"]

    assert main(["--workspace", str(ws.root), "decide", "demo", "novelty", "approve"]) == 2  # сначала выбор A/B
    assert main(["--workspace", str(ws.root), "decide", "demo", "novelty", "a"]) == 0
    cur = project.artifact("novelty")
    assert cur.version == 3 and cur.status == "in_review" and "Вариант B" not in cur.body and cur.meta["created_by"]["provider"] == "writer"
    assert cur.meta["reviews"] and "ab" not in cur.meta and cur.meta["ab_chosen"]["chosen"] == "a"
    assert main(["--workspace", str(ws.root), "decide", "demo", "novelty", "a"]) == 2  # A/B уже завершён

    run_task(ws, enqueue(ws, "demo", "novelty", {"ab": "writer-b"}))
    client = TestClient(create_app(ws, worker=False))
    assert "выбрать B" in client.get("/projects/demo/artifacts/novelty").text
    assert "A/B с writer-b" in client.get("/projects/demo").text
    assert client.post("/projects/demo/artifacts/novelty/ab/b", follow_redirects=False).status_code == 303
    assert "Вариант B" in project.artifact("novelty").body

    with closing(ws.db()) as db:
        table, _ = summary(db, "ideator")
    by = {t["provider"]: t["ab"] for t in table}
    assert by == {"writer": (1, 2), "writer-b": (1, 2)}
    assert "1 из 2" in client.get("/bench?role=ideator").text


def test_ab_rejects_same_provider_and_unsupported_stage(tmp_path):
    ws, _ = make(tmp_path, AB_POOL)
    res = run_task(ws, enqueue(ws, "demo", "novelty", {"input": "x", "ab": "writer"}))
    assert "отличная" in res["error"]
    res = run_task(ws, enqueue(ws, "demo", "review", {"artifact": "novelty", "ab": "writer-b"}, gate=False))
    assert "только у стадий" in res["error"]


def test_reindex_restores_ab_choices(tmp_path):
    from workflow_writing_paper.core.maintenance import reindex

    ws, _ = make(tmp_path, AB_POOL)
    run_task(ws, enqueue(ws, "demo", "novelty", {"input": "x", "ab": "writer-b"}))
    assert main(["--workspace", str(ws.root), "decide", "demo", "novelty", "b"]) == 0
    with closing(ws.db()) as db:
        db.execute("DELETE FROM ab_choices")
        db.commit()
    assert reindex(ws)["ab_choices"] == 1 and reindex(ws)["ab_choices"] == 0


def test_ab_b_failure_and_unknown_provider_keep_a(tmp_path):
    ws, project = make(tmp_path, AB_POOL)
    res = run_task(ws, enqueue(ws, "demo", "novelty", {"input": "x", "ab": "nope"}))
    assert "нет в пуле" in res["error"] and project.artifact("novelty") is None  # A не запускалась
    res = run_task(ws, enqueue(ws, "demo", "novelty", {"input": "x", "ab": "writer-dead"}))
    assert "A готова (v1)" in res["error"] and "пустой ответ" in res["error"]
    cur = project.artifact("novelty")
    assert cur.version == 1 and "ab" not in cur.meta and cur.meta["created_by"]["provider"] == "writer"


def test_ab_draft_b_sees_draft_before_a(tmp_path):
    import json

    from test_pipeline import SECTION_OK, set_reply

    from workflow_writing_paper.registry import add_manual

    ws, project = make(tmp_path, AB_POOL)
    run_task(ws, enqueue(ws, "demo", "novelty", {"input": "x"}))
    project.decide("novelty", "approve")
    project.skip_stage("literature", "тест")
    project.skip_stage("experiment_plan", "тест")
    add_manual(project.path / "results.json", "latency_p50_ms", 103.32, "ms")
    add_manual(project.path / "results.json", "peak_ram_mb", 512, "MB")
    set_reply(ws, "writer", SECTION_OK.replace("Медианная", "Текст A: медианная"))
    set_reply(ws, "writer-b", SECTION_OK)
    res = run_task(ws, enqueue(ws, "demo", "draft", {"section": "Эксперимент", "ab": "writer-b"}))
    assert res["error"] is None, res
    b_calls = [json.loads(p.read_text()) for p in sorted((project.path / "calls").glob("*writer-b.json"))]
    assert b_calls and all("Текст A" not in m["content"] for c in b_calls for m in c["messages"])
    assert "Текст A" not in project.artifact("draft").body
