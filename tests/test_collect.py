"""Шаблон explore: сбор литературы по теме → одобрение корпуса → обзор → новизна из пробелов."""

import io
import json
from pathlib import Path

import yaml

from workflow_writing_paper.core import Project, Workspace
from workflow_writing_paper.core.engine import enqueue, run_task
from workflow_writing_paper.library import openalex


def _work(wid, title, score_refs=(), pdf=""):
    return {"id": f"https://openalex.org/{wid}", "doi": f"10.1/{wid.lower()}", "title": title, "year": "2024",
            "authors": [f"Author {wid}"], "venue": "J", "type": "article", "abstract": f"Abstract of {title}",
            "cited_by": 10, "refs": [f"https://openalex.org/{r}" for r in score_refs], "pdf_urls": [pdf] if pdf else [],
            "retracted": False}


SEARCH = [_work("W1", "Plant disease detection in the field", ["W3"], pdf="https://example.org/w1.pdf"),
          _work("W2", "Unrelated topic about finance"),
          {**_work("W9", "Retracted study"), "retracted": True}]
REFS = [_work("W3", "Leaf images from smartphones"), _work("W1", "Plant disease detection in the field")]
SCORES = {"queries": ["plant disease field", "leaf classification"],
          "scores": [{"id": "W1", "score": 9, "reason": "прямо про тему"}, {"id": "W2", "score": 2, "reason": "нет"},
                     {"id": "W3", "score": 8, "reason": "данные с телефона"}]}
POOL = {
    "providers": [
        {"id": "scout", "kind": "fake", "family": "a", "json_reply": SCORES, "reply": "обзор"},
        {"id": "writer", "kind": "fake", "family": "a", "reply": "новизна"},
        {"id": "crit", "kind": "fake", "family": "b", "reply": "Замечание."},
        {"id": "judge-x", "kind": "fake", "family": "x", "score": 8},
        {"id": "judge-y", "kind": "fake", "family": "y", "score": 9},
    ],
    "roles": {"literature_scout": "scout", "ideator": "writer", "critic": "crit", "judge": ["judge-x", "judge-y"]},
}


def _patch_openalex(monkeypatch, calls):
    monkeypatch.setattr(openalex, "search", lambda q, limit, mailto: (calls.append(q), SEARCH)[1])
    monkeypatch.setattr(openalex, "by_ids", lambda ids, mailto: (calls.append(tuple(ids)), REFS)[1])

    def fake_download(url, dst, max_bytes):
        assert url == "https://example.org/w1.pdf"
        dst.write_bytes(b"%PDF-1.4 not a real pdf")
        return True
    monkeypatch.setattr(openalex, "download_pdf", fake_download)


def test_explore_collect_approve_literature_novelty(tmp_path, monkeypatch):
    calls: list = []
    _patch_openalex(monkeypatch, calls)
    ws = Workspace(tmp_path / "ws")
    ws.init()
    (ws.root / "providers.yaml").write_text(yaml.safe_dump(POOL, allow_unicode=True), encoding="utf-8")
    project = Project.create(ws, "topic", template="explore")
    assert project.manifest["library"] == "project" and project.library().root == project.path / "library"
    assert project.can_run("literature")[0] is False  # корпус ещё не собран

    res = run_task(ws, enqueue(ws, "topic", "collect", {"input": "Болезни растений по снимкам с телефона"}))
    assert res["error"] is None and res["found"] == 3 and res["selected"] == 2  # W2 отсеян, W9 отозван
    assert calls[:2] == SCORES["queries"] and ("https://openalex.org/W3",) in calls  # снежный ком по ссылкам W1
    corpus = project.artifact("corpus")
    keys = [w["key"] for w in corpus.meta["works"]]
    assert corpus.status == "in_review" and [w["score"] for w in corpus.meta["works"]] == [9, 8]
    lib = project.library()
    assert set(keys) <= lib.citable_keys() and (lib.pdf_dir / f"{keys[0]}.pdf").exists()
    assert lib.search("smartphones")[0]["key"] == keys[1]  # без PDF ищется по аннотации
    assert not (ws.root / "library" / "refs.bib").exists()  # общая библиотека не тронута

    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    client = TestClient(create_app(ws, worker=False))
    page = client.get("/projects/topic/artifacts/corpus").text
    assert "вычеркнуть" in page and "перепроверить судьями" not in page and 'href="https://openalex.org/W1"' in page
    assert client.get("/projects/topic").status_code == 200
    r = client.post(f"/projects/topic/corpus/exclude/{keys[1]}", follow_redirects=False)
    assert r.status_code == 303
    assert client.post("/projects/topic/corpus/exclude/nope", follow_redirects=False).status_code == 400
    corpus = project.artifact("corpus")
    assert [w["key"] for w in corpus.meta["works"]] == keys[:1] and keys[1] not in lib.citable_keys()
    assert f"Вычеркнуто автором: {keys[1]}" in corpus.body
    project.decide("corpus", "approve")
    assert project.stage_state("collect") == "done" and project.can_run("literature")[0]

    review = ("## Позиционирование\nТема.\n## Карта близких работ\nПолевые снимки " + f"[@{keys[0]}].\n"
              "## Оценка новизны\nЧастично.\n## Пробелы\nНет данных с телефона.\n")
    cfg = ws.config()
    cfg["providers"][0]["reply"] = review
    ws.save_config(cfg)
    res = run_task(ws, enqueue(ws, "topic", "literature"))
    assert res["error"] is None and res["accepted"] and res["openalex_suggested"] == 0
    project.decide("literature", "approve")

    res = run_task(ws, enqueue(ws, "topic", "novelty"))  # без абзаца автора: тема корпуса + одобренный обзор
    assert res["error"] is None
    novelty = project.artifact("novelty")
    assert novelty.meta["source"] == "Болезни растений по снимкам с телефона"
    record = Path(novelty.meta["created_by"]["call"]).read_text(encoding="utf-8")
    prompt = json.loads(record)["messages"][1]["content"]
    assert "Одобренный обзор литературы" in prompt and "Пробелы" in prompt


def test_download_pdf_rejects_non_pdf_oversize_and_scheme(tmp_path, monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = {"html": b"<html>landing</html>", "big": b"%PDF-" + b"x" * 5000, "ok": b"%PDF-1.7 tiny"}
    monkeypatch.setattr(openalex.urllib.request, "urlopen",
                        lambda req, timeout: Resp(payload[req.full_url.rsplit("/", 1)[-1]]))
    dst = tmp_path / "a.pdf"
    assert not openalex.download_pdf("https://x.org/html", dst) and not dst.exists()
    assert not openalex.download_pdf("https://x.org/big", dst, max_bytes=1000) and not dst.exists()
    assert not openalex.download_pdf("file:///etc/passwd", dst) and not dst.exists()
    assert openalex.download_pdf("https://x.org/ok", dst) and dst.read_bytes().startswith(b"%PDF-")
    assert not list(tmp_path.glob("*.part"))


def test_abstract_from_inverted_index():
    assert openalex.abstract({"world": [1], "Hello": [0], "again": [2]}) == "Hello world again"
    assert openalex.abstract(None) == ""
    w = openalex.work_summary({"id": "https://openalex.org/W1", "abstract_inverted_index": {"x": [0]},
                               "best_oa_location": {"pdf_url": "https://a/b.pdf"}, "is_retracted": True,
                               "locations": [{"pdf_url": "https://c/d.pdf"}, {"pdf_url": "https://a/b.pdf"}, {}],
                               "referenced_works": ["https://openalex.org/W2"], "cited_by_count": 3})
    assert (w["abstract"], w["pdf_urls"], w["retracted"], w["refs"], w["cited_by"]) == \
        ("x", ["https://a/b.pdf", "https://c/d.pdf"], True, ["https://openalex.org/W2"], 3)


def test_screen_splits_batch_when_model_breaks_json(monkeypatch):
    from types import SimpleNamespace

    from workflow_writing_paper import roles
    from workflow_writing_paper.roles.collect import _screen

    def fake_call(project, role, user, task, max_tokens, json_schema):
        ids = [ln.split()[2] for ln in user.splitlines() if ln.startswith("- id ")]
        if len(ids) > 1 or ids == ["W4"]:
            raise roles.StageError("ответ не JSON после 2 повторов")
        return SimpleNamespace(data={"scores": [{"id": ids[0], "score": 7, "reason": "ок"}]}), None
    monkeypatch.setattr(roles, "call_role", fake_call)
    works = [_work(f"W{i}", f"t{i}") for i in range(1, 5)]
    assert _screen(None, "тема", works, None) == 1
    assert [w["score"] for w in works] == [7, 7, 7, 0] and works[3]["reason"] == "модель не оценила"
