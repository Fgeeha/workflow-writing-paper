"""Сквозной сценарий v0.2 на провайдере fake: новизна → проверка семействами → одобрение → раздел черновика."""

import json
from pathlib import Path

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


def make(tmp_path: Path, pool: dict | None = None) -> tuple[Workspace, Project]:
    pool = pool or POOL
    ws = Workspace(tmp_path / "ws")
    ws.init()
    (ws.root / "providers.yaml").write_text(yaml.safe_dump(pool, allow_unicode=True), encoding="utf-8")
    return ws, Project.create(ws, "demo", template="experimental")


def set_reply(ws: Workspace, pid: str, reply: str) -> None:
    cfg = ws.config()
    next(p for p in cfg["providers"] if p["id"] == pid)["reply"] = reply
    ws.save_config(cfg)


def test_end_to_end_novelty_then_draft(tmp_path):
    ws, project = make(tmp_path)
    try:
        enqueue(ws, "demo", "draft", {"section": "Эксперимент"})
        raise AssertionError("гейт не сработал")
    except ValueError as e:
        assert "«новизна» не завершена" in str(e)

    tid = enqueue(ws, "demo", "novelty", {"input": "Мы ускорили инференс на одноплатном компьютере."})
    assert run_pending(ws) == 1
    task = ws.db().execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert task["status"] == "done", task["error"]
    art = project.artifact("novelty")
    assert art.version == 1 and art.status == "in_review"
    assert art.meta["created_by"]["family"] == "a"
    assert art.meta["source"].startswith("Мы ускорили") and art.meta["created_by"]["call"].endswith(".json")
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
    try:
        project.skip_stage("draft", "нельзя")
        raise AssertionError("обязательную стадию пропустили")
    except ValueError:
        pass


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
            "F1 = 0,82, задержка 103,3 мс, выборка 1200 изображений [Иванов, 2021] (Smith et al., 2020a).\n"
            "`code 999` и формула $x = 42.5$ не считаются.\nТочность 97,1 % — {pending}.\nПлохо: 0,65.\n"
            "Скобки не прячут числа: (задержка 95,5 мс на 1000 снимках).\n")
    bl = check_numbers(text, reg, rounding=2)
    assert [b["number"] for b in bl] == ["0,65", "95,5", "1000"]
    assert check_numbers("Параметров 3,51 млн, операций 300,8 млн.", {"p": {"value": 3.505}, "m": {"value": 300.8}}) == []
    big = check_numbers("Задержка 95,5 мс.", {"flops": {"value": 1e27}, "x": {"value": float("inf")}})
    assert [b["number"] for b in big] == ["95,5"]  # большие и бесконечные значения не роняют проверку


def test_merge_section_order_and_replace():
    order = ["Введение", "Метод", "Заключение"]
    body = merge_section("", "Метод", "Текст метода.", order)
    body = merge_section(body, "Введение", "## Введение\n\nВступление.", order)
    assert body.index("## Введение") < body.index("## Метод")
    body = merge_section(body, "Метод", "## Метод\n\nНовый текст.", order)
    assert body.count("## Метод") == 1 and "Новый текст" in body and "Текст метода" not in body
    import pytest

    from workflow_writing_paper.roles import StageError
    for empty in ("", "## Метод", "## Метод\n\n  "):  # пустой ответ модели не затирает написанный раздел
        with pytest.raises(StageError):
            merge_section(body, "Метод", empty, order)


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
    assert "novelty" in page and "«новизна» не завершена (не начата)" in page
    assert "<td>новизна</td>" in page and "не начата" in page  # подписи стадий и состояний — по-русски
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
    # ручная правка: новая версия от человека; правка устаревшей версии — 409
    assert "Редактировать текст" in client.get("/projects/web-demo/artifacts/novelty").text
    r = client.post("/projects/web-demo/artifacts/novelty/edit", data={"version": "1", "body": "Правка.\r\nВторая."},
                    follow_redirects=False)
    from workflow_writing_paper.core.project import Project
    edited = Project(ws, ws.root / "projects" / "web-demo").artifact("novelty")
    assert r.status_code == 303 and edited.version == 2 and edited.body.startswith("Правка.\nВторая.")
    assert edited.meta["created_by"]["role"] == "human" and edited.status == "draft"
    assert client.post("/projects/web-demo/artifacts/novelty/edit", data={"version": "1", "body": "x"}).status_code == 409
    assert client.post("/projects/web-demo/artifacts/corpus/edit", data={"version": "1", "body": "x"}).status_code == 400
    edited.status = "in_review"  # на проверке, но судьи ещё не оценивали — главная не должна падать
    Project(ws, ws.root / "projects" / "web-demo").update_artifact(edited)
    assert client.get("/").status_code == 200
    # файлы статьи: только из белого списка, несобранный — 404
    assert client.get("/projects/web-demo/files/paper.pdf").status_code == 404
    assert client.get("/projects/web-demo/files/paper-workflow.yaml").status_code == 404
    assert client.get("/projects/web-demo/files/..%2F..%2Fenv").status_code == 404
    (ws.root / "projects" / "web-demo" / "paper.pdf").write_bytes(b"%PDF-1.4 test")
    r = client.get("/projects/web-demo/files/paper.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
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
    journal = ws.root / "journals" / "generic.yaml"  # путь до сборки, а не лимиты: минимум источников снят
    journal.write_text(journal.read_text(encoding="utf-8").replace("references_min: 15", "references_min: 0"),
                       encoding="utf-8")
    body = "\n\n".join(f"## {s}\n\nТекст раздела без чисел." for s in project.sections())
    project.write_artifact("draft", body, by)
    assert project.stage_state("draft") == "complete"  # написан, но не одобрен
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


def test_review_locates_quotes_threshold_and_budget(tmp_path):
    from workflow_writing_paper.review import _locate, review_artifact

    body = "## Введение\n\nПервая строка.\nЗадержка 95 мс без источника.\n"
    bl = _locate([{"quote": "задержка 95 мс", "issue": "x"}, {"quote": "нет такого", "issue": "y"}], body)
    assert bl[0]["line"] == 4 and bl[1]["line"] is None

    ws, project = make(tmp_path)
    project.manifest["review"]["threshold"] = 8.5  # манифест перекрывает порог рубрики (7.0)
    project.save_manifest()
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"})
    res = review_artifact(project, "novelty")
    assert res["threshold"] == 8.5 and not res["accepted"] and res["min_overall"] == 8.0

    project.manifest["budget"] = {"usd": 0.0}
    project.save_manifest()
    cfg = ws.config()
    for p in cfg["providers"]:
        p["price"] = {"input_per_1m": 1.0, "output_per_1m": 1.0}
    ws.save_config(cfg)
    try:
        review_artifact(project, "novelty")
        raise AssertionError("проверка пошла при исчерпанном бюджете")
    except Exception as e:  # noqa: BLE001
        assert "оценка стоимости" in str(e)


def test_novelty_rerun_uses_author_source_not_model_text(tmp_path):
    ws, project = make(tmp_path)
    run_task(ws, enqueue(ws, "demo", "novelty", {"input": "Исходный абзац автора."}))
    run_task(ws, enqueue(ws, "demo", "novelty", {}))  # перезапуск без ввода
    art = project.artifact("novelty")
    assert art.version == 2 and art.meta["source"] == "Исходный абзац автора."
    ws2, _ = make(tmp_path / "second")
    res = run_task(ws2, enqueue(ws2, "demo", "novelty", {}))
    assert "нужен абзац новизны" in res["error"]


def test_draft_limits_and_cross_refs_only_when_complete(tmp_path):
    from workflow_writing_paper.review import agreed_first
    from workflow_writing_paper.review.checks import cross_refs, deterministic_checks

    body = ("Как видно из табл. 1 и рис. 2, всё сходится; см. также таблицу 3.\n\n"
            "**Таблица 1.** Задержка\n\n![Рисунок 2 — схема](x.png)\n\nРисунок 4. Без ссылки\n")
    issues = sorted(b["issue"] for b in cross_refs(body))
    assert issues == ["на рисунок 4 нет ссылки в тексте", "ссылка на табл. 3, но подписи «Таблица 3» нет"]
    assert cross_refs("Риски 2 и 3 — не ссылки на рисунки.") == []
    assert cross_refs("Таблица 1 показывает рост")[0]["issue"].startswith("ссылка на табл. 1")  # не подпись

    ws, project = make(tmp_path)
    by = {"role": "section_writer", "provider": "writer", "family": "a"}
    rubric = ws.text("rubrics/draft.md")
    sections = project.sections()
    project.write_artifact("draft", f"## {sections[0]}\n\nСм. табл. 7.\n", by)  # неполный черновик
    assert not [b for b in deterministic_checks(project, project.artifact("draft"), rubric)
                if "табл" in b["issue"] or "источников" in b["issue"]]
    full = "\n\n".join(f"## {s}\n\nСм. табл. 7." for s in sections)
    project.write_artifact("draft", "## Аннотация\n\n" + "слово " * 300 + "\n\n" + full, by)
    issues = [b["issue"] for b in deterministic_checks(project, project.artifact("draft"), rubric)]
    assert any("аннотация 300 слов" in i for i in issues)
    assert any("источников 0 при минимуме журнала 15" in i for i in issues)
    assert any("подписи «Таблица 7» нет" in i for i in issues)

    bl = agreed_first([{"judge": "a", "line": 3, "issue": "x"}, {"judge": "b", "line": "5–7", "issue": "y"},
                       {"judge": "b", "line": 3, "issue": "z"}, {"judge": "checker", "line": 9, "issue": "w"}])
    assert [b["issue"] for b in bl] == ["x", "z", "y", "w"] and bl[0]["agreed"] and not bl[2]["agreed"]


def test_cli_profile_assigns_roles(tmp_path, capsys):
    from workflow_writing_paper.cli import main

    ws, _ = make(tmp_path, {**POOL, "profiles": {"fast": {"ideator": "crit", "judge": ["judge-x", "judge-y"]}}})
    assert main(["--workspace", str(ws.root), "profile", "fast"]) == 0
    roles = ws.config()["roles"]
    assert roles["ideator"] == "crit" and roles["judge"] == ["judge-x", "judge-y"] and roles["critic"] == "crit"
    assert "judge: judge-x, judge-y" in capsys.readouterr().out
    assert main(["--workspace", str(ws.root), "profile", "nope"]) == 2


def test_start_from_scratch_creates_explore_project_and_queues_collect(tmp_path):
    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    ws, _ = make(tmp_path)
    client = TestClient(create_app(ws, worker=False))
    assert "Начать с нуля" in client.get("/").text
    assert client.post("/start", data={"topic": "коротко"}).status_code == 400
    topic = "Распознавание болезней листьев на одноплатном компьютере: перенос с лабораторных снимков на полевые."
    r = client.post("/start", data={"topic": topic}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/projects/raspoznavanie-boleznei-listev"
    r2 = client.post("/start", data={"topic": topic, "title": "Вторая"}, follow_redirects=False)
    assert r2.headers["location"].endswith("-2")
    project = Project.open(ws, "raspoznavanie-boleznei-listev")
    assert project.manifest["template"] == "explore" and project.manifest["title"].startswith("Распознавание")
    with ws.db() as db:
        task = db.execute("SELECT stage, payload, status FROM tasks WHERE project = ?", (project.slug,)).fetchone()
    assert task["stage"] == "collect" and task["status"] == "queued" and topic in json.loads(task["payload"])["input"]


def test_htmx_served_locally(tmp_path):
    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    ws, _ = make(tmp_path)
    client = TestClient(create_app(ws, worker=False))
    assert 'src="/static/htmx.min.js"' in client.get("/").text
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200 and r.text.startswith("var htmx")


def test_approve_blocked_by_blockers_and_pending_blocks_assembly(tmp_path):
    from workflow_writing_paper.roles import StageError, stage_assembly, truncated_revision

    _ws, project = make(tmp_path)
    by = {"role": "section_writer", "provider": "writer", "family": "a"}
    art = project.write_artifact("draft", "## Введение\n\nF1 = {pending}.", by)
    art.meta["review_summary"] = {"accepted": False, "blockers": 2}
    project.update_artifact(art)
    try:
        project.decide("draft", "approve")
        raise AssertionError("approve с блокерами")
    except ValueError as e:
        assert "override" in str(e)
    project.decide("draft", "override", "автор принял риск")
    try:
        stage_assembly(project, {})
        raise AssertionError("assembly с {pending}")
    except StageError as e:
        assert "{pending}" in str(e) and "3: F1" in str(e)
    assert truncated_revision("x" * 1000, "заменить A на B") and not truncated_revision("x" * 1000, "y" * 900)


def test_title_from_topic_cut_at_word(tmp_path):
    _ws, project = make(tmp_path)
    project.set_title_from("слово " * 40 + "\nвторая строка")
    title = project.manifest["title"]
    assert len(title) <= 121 and title.endswith("слово…")
    project.set_title_from("другая тема")  # уже задан — не перезаписывается
    assert project.manifest["title"] == title
