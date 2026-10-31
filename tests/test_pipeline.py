"""Сквозной сценарий v0.2 на провайдере fake: новизна → проверка семействами → одобрение → раздел черновика."""

import json

import yaml

from workflow_writing_paper.core import Project, Workspace
from workflow_writing_paper.core.engine import enqueue, run_pending, run_task
from workflow_writing_paper.registry import add_manual, check_numbers
from workflow_writing_paper.roles import merge_section

NOVELTY_OK = """## Формулировки новизны
1. Осторожная. 2. Сильная.
## Исследовательские вопросы
Вопрос.
## Гипотезы
Гипотеза; опровергается, если точность ниже базовой.
## Утверждения
Утверждение — подтверждается экспериментом.
## Возражения
Возражение и ответ.
## Журналы
Профильный журнал.
"""

SECTION_OK = "## Эксперимент\n\nМедианная задержка составила 103,32 мс при пиковой памяти 512 МБ, F1 = {pending}.\n"
SECTION_BAD = "## Эксперимент\n\nЗадержка составила 95,5 мс, в современном мире это важно.\n"

POOL = {
    "providers": [
        {"id": "writer", "kind": "fake", "family": "a", "reply": NOVELTY_OK},
        {"id": "crit", "kind": "fake", "family": "b", "reply": "Замечание: уточнить выборку."},
        {"id": "judge-x", "kind": "fake", "family": "x", "score": 8},
        {"id": "judge-y", "kind": "fake", "family": "y", "score": 9},
        {"id": "judge-a", "kind": "fake", "family": "a", "score": 10},  # семейство автора — не судит
    ],
    "roles": {"ideator": "writer", "critic": "crit", "section_writer": "writer",
              "judge": ["judge-a", "judge-x", "judge-y"]},
}


def make(tmp_path, pool=POOL):
    ws = Workspace(tmp_path / "ws")
    ws.init()
    (ws.root / "providers.yaml").write_text(yaml.safe_dump(pool, allow_unicode=True), encoding="utf-8")
    return ws, Project.create(ws, "demo", template="experimental")


def set_reply(ws, pid, reply):
    cfg = ws.config()
    next(p for p in cfg["providers"] if p["id"] == pid)["reply"] = reply
    ws.save_config(cfg)


def test_end_to_end_novelty_then_draft(tmp_path):
    ws, project = make(tmp_path)
    try:
        enqueue(ws, "demo", "draft", {"section": "Эксперимент"})
        raise AssertionError("гейт не сработал")
    except ValueError as e:
        assert "novelty" in str(e)

    tid = enqueue(ws, "demo", "novelty", {"input": "Мы ускорили инференс на одноплатном компьютере."})
    assert run_pending(ws) == 1
    task = ws.db().execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert task["status"] == "done", task["error"]
    art = project.artifact("novelty")
    assert art.version == 1 and art.status == "in_review"
    assert art.meta["created_by"]["family"] == "a"
    assert art.meta["source"].startswith("Мы ускорили")
    judges = {json.loads((project.path / p).read_text())["judge"]["provider"] for p in art.meta["reviews"]}
    assert judges == {"judge-x", "judge-y", "checker"}  # judge-a (семейство автора) исключён
    assert art.meta["review_summary"]["accepted"] and art.meta["review_summary"]["min_overall"] == 8.0
    assert ws.db().execute("SELECT count(*) FROM calls WHERE project='demo'").fetchone()[0] == 5  # 3 роли + 2 судьи

    project.decide("novelty", "approve")
    assert project.artifact("novelty").status == "approved" and project.stage_state("novelty") == "done"
    project.skip_stage("literature", "v0.3")
    project.skip_stage("experiment_plan", "v0.3")

    add_manual(project.path / "results.json", "latency_p50_ms", 103.32, "ms")
    add_manual(project.path / "results.json", "peak_ram_mb", 512, "MB")
    set_reply(ws, "writer", SECTION_OK)
    tid = enqueue(ws, "demo", "draft", {"section": "Эксперимент"})
    res = run_task(ws, tid)
    assert res["error"] is None and res["accepted"], res
    draft = project.artifact("draft")
    assert "## Эксперимент" in draft.body and draft.status == "in_review"
    assert project.stage_state("draft") == "in_progress"  # не все разделы журнала написаны


def test_checker_blocks_number_not_in_registry_and_banned_phrase(tmp_path):
    ws, project = make(tmp_path)
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"})
    project.decide("novelty", "approve")
    for st in ("literature", "experiment_plan"):
        project.skip_stage(st, "тест")
    add_manual(project.path / "results.json", "latency_p50_ms", 103.32, "ms")
    set_reply(ws, "writer", SECTION_BAD)  # писатель упорно возвращает плохой текст
    res = run_task(ws, enqueue(ws, "demo", "draft", {"section": "Эксперимент"}))
    assert res["error"] is None and not res["accepted"]
    issues = [b["issue"] for b in res["blockers"]]
    assert any("95,5" in i for i in issues) and any("запрещённая фраза" in i for i in issues)
    assert project.artifact("draft").version == 3  # 1 запись + 2 автоправки, дальше — человек
    assert ws.db().execute("SELECT count(*) FROM tasks WHERE status='done'").fetchone()[0] == 1


def test_check_numbers_rules():
    reg = {"f1": {"value": 0.8234}, "lat": {"value": 103.32}, "n": {"value": 1200}}
    text = ("## Результаты 2024\n1. Три прогона, 5 моделей.\n"
            "F1 = 0,82, задержка 103,3 мс, выборка 1200 изображений [Иванов, 2021].\n"
            "`code 999` и формула $x = 42.5$ не считаются.\nТочность 97,1 % — {pending}.\nПлохо: 0,65.\n")
    bl = check_numbers(text, reg, rounding=2)
    assert [b["number"] for b in bl] == ["0,65"]


def test_merge_section_order_and_replace():
    order = ["Введение", "Метод", "Заключение"]
    body = merge_section("", "Метод", "Текст метода.", order)
    body = merge_section(body, "Введение", "## Введение\n\nВступление.", order)
    assert body.index("## Введение") < body.index("## Метод")
    body = merge_section(body, "Метод", "## Метод\n\nНовый текст.", order)
    assert body.count("## Метод") == 1 and "Новый текст" in body and "Текст метода" not in body


def test_stale_and_decisions(tmp_path):
    ws, project = make(tmp_path)
    by = {"role": "ideator", "provider": "writer", "family": "a"}
    project.write_artifact("novelty", NOVELTY_OK, by)
    project.decide("novelty", "approve")
    project.write_artifact("draft", "## Введение\n\nТекст.", by)
    project.decide("draft", "override", "принято с оговоркой")
    assert project.artifact("draft").meta["override_reason"] == "принято с оговоркой"
    project.write_artifact("novelty", NOVELTY_OK + "\nПравка.", by)  # новая версия новизны
    assert project.artifact("draft").status == "stale" and project.stage_state("review") == "stale"
    assert (project.path / "artifacts" / "history" / "novelty.v1.md").exists()
    rows = ws.db().execute("SELECT decision FROM approvals ORDER BY id").fetchall()
    assert [r["decision"] for r in rows] == ["approve", "override"]


def test_web_pages_end_to_end(tmp_path):
    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    ws, _ = make(tmp_path)
    client = TestClient(create_app(ws, worker=False))
    assert client.get("/").status_code == 200
    r = client.post("/projects", data={"slug": "web-demo", "template": "survey", "journal": "generic",
                                       "language": "ru"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/projects/web-demo").text
    assert "novelty" in page and "pending" in page
    r = client.post("/projects/web-demo/run/novelty", data={"input": "Идея."}, follow_redirects=False)
    assert r.status_code == 303 and run_pending(ws) == 1
    assert client.post("/projects/web-demo/run/draft", data={"section": "Метод"}).status_code == 400  # гейт
    home = client.get("/").text
    assert "web-demo" in home and "решить" in home
    art_page = client.get("/projects/web-demo/artifacts/novelty").text
    assert "judge-x" in art_page and "checker" in art_page and "одобрить" in art_page
    assert client.post("/projects/web-demo/artifacts/novelty/decide", data={"decision": "override"}).status_code == 400
    r = client.post("/projects/web-demo/artifacts/novelty/decide", data={"decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/projects/web-demo/artifacts/novelty/to-eval", follow_redirects=False).status_code == 303
    assert list((ws.root / "eval" / "tasks" / "ideator").glob("ideator-web-demo-*.yaml"))
    r = client.post("/projects/web-demo/registry", data={"key": "f1", "value": "0,82", "unit": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and "0.82" in client.get("/projects/web-demo/registry").text
    assert client.post("/projects/web-demo/registry", data={"key": "x", "value": "abc"}).status_code == 400
    assert "prompts/ru/judge.md" in client.get("/settings").text
    assert client.get("/settings/edit?rel=../etc/passwd").status_code == 400
    r = client.post("/settings/edit", data={"rel": "rubrics/novelty.md", "text": "# x\n| C1 | a | 1 |\n"},
                    follow_redirects=False)
    assert r.status_code == 303 and (ws.root / "rubrics" / "novelty.md").read_text().startswith("# x")


def test_required_sections_match_by_stem(tmp_path):
    from workflow_writing_paper.core.artifact import Artifact
    from workflow_writing_paper.review.checks import deterministic_checks

    ws, project = make(tmp_path)
    rubric = ws.text("rubrics/novelty.md")
    body = NOVELTY_OK.replace("## Журналы", "### 6. Типы журналов и конференций").replace("## Гипотезы",
                                                                                          "### Гипотезы и критерии")
    assert deterministic_checks(project, Artifact(type="novelty", body=body), rubric) == []
    issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="novelty", body="## Гипотезы\nx"), rubric)]
    assert len(issues) == 5 and all("нет обязательного раздела" in i for i in issues)


def test_failed_judge_is_replaced_by_next_candidate(tmp_path):
    from workflow_writing_paper.review import review_artifact

    pool = {**POOL, "providers": [*POOL["providers"], {"id": "judge-bad", "kind": "fake", "family": "z", "bad_json": 9}]}
    pool["roles"] = {**POOL["roles"], "judge": ["judge-bad", "judge-x", "judge-y"]}
    _ws, project = make(tmp_path, pool)
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"})
    res = review_artifact(project, "novelty")
    assert res["accepted"] and set(res["reports"]) == {"judge-bad", "judge-x", "judge-y", "checker"}
    assert res["reports"]["judge-bad"]["error"] and len(res["errors"]) == 1


def test_review_and_assembly_stages(tmp_path):
    ws, project = make(tmp_path)
    by = {"role": "section_writer", "provider": "writer", "family": "a"}
    project.write_artifact("novelty", NOVELTY_OK, by)
    project.decide("novelty", "approve")
    for st in ("literature", "experiment_plan"):
        project.skip_stage(st, "тест")
    body = "\n\n".join(f"## {s}\n\nТекст раздела без чисел." for s in project.sections())
    project.write_artifact("draft", body, by)
    assert project.stage_state("draft") == "done"
    res = run_task(ws, enqueue(ws, "demo", "review"))
    assert res["error"] is None and res["accepted"]
    assert project.artifact("draft").status == "in_review"
    try:
        enqueue(ws, "demo", "assembly")
        raise AssertionError("assembly до одобрения черновика")
    except ValueError:
        pass
    project.decide("draft", "approve")
    res = run_task(ws, enqueue(ws, "demo", "assembly"))
    assert res["error"] is None and "paper.md" in res["outputs"]
    assert (project.path / "paper.md").read_text().startswith("# demo")
    assert project.artifact("paper").status == "approved" and project.stage_state("assembly") == "done"
    # перепроверка любого артефакта без гейта
    res = run_task(ws, enqueue(ws, "demo", "review", {"artifact": "novelty"}, gate=False))
    assert res["error"] is None and res["accepted"]
