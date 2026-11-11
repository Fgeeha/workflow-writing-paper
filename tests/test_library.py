"""v0.3: библиотека (BibTeX, PDF, FTS5, OpenAlex), проверка ключей цитирования, стадии literature и experiment_plan."""

import shutil

import pytest
import yaml
from test_pipeline import NOVELTY_OK, POOL, make, set_reply

from workflow_writing_paper.core import Project, Workspace
from workflow_writing_paper.core.artifact import Artifact
from workflow_writing_paper.core.engine import enqueue, run_task
from workflow_writing_paper.library import Library, LibraryError, is_stub, openalex
from workflow_writing_paper.library.bibtex import make_key, normalize_doi, parse
from workflow_writing_paper.review import _context
from workflow_writing_paper.review.checks import citation_keys, deterministic_checks

BIB = """
@comment{служебная запись пропускается}
@article{smith2020deep,
  title = {Deep {Learning} on the Edge: A Survey},
  author = {Smith, John and Doe, Jane},
  year = 2020,
  journal = "Journal of Edge Computing",
  doi = {https://doi.org/10.1000/EDGE.2020.1}
}
@inproceedings{ivanov2021plant,
  title={Распознавание болезней растений на одноплатных компьютерах},
  author={Иванов, Пётр},
  year={2021},
  booktitle={Труды конференции}
}
"""

LITERATURE_OK = """## Позиционирование
Работа относится к инференсу на краевых устройствах [@smith2020deep].
## Карта близких работ
Группа 1: обзоры [@smith2020deep]; группа 2: прикладные работы [@ivanov2021plant].
## Оценка новизны
Формулировка подтверждается: по аннотации [@ivanov2021plant] задержка не измерялась.
## Пробелы
Не хватает работ по квантованию; запрос для OpenAlex: «post-training quantization edge inference».
"""

PLAN_OK = """## Эксперименты
| Утверждение | Эксперимент | Метрика | Прогоны |
|---|---|---|---|
| Ускорение | замер задержки на 500 снимках | p50, мс | 5 сидов |
## Базовые линии
FP32-модель без оптимизаций.
## Ключи реестра
`latency_p50_ms` (мс, сценарий bench), `f1_zero_shot` (ручной ввод).
## Риски
Нагрев платы; сначала замер базовой линии.
"""


def pdf_bytes(text: str) -> bytes:
    """Минимальный одностраничный PDF с латинским текстом; pdftotext восстанавливает xref сам."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
             b"/Resources << /Font << /F1 5 0 R >> >> >>"),
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    body = b"%PDF-1.4\n" + b"".join(b"%d 0 obj\n" % (i + 1) + o + b"\nendobj\n" for i, o in enumerate(objs))
    return body + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def lib_pool() -> dict:
    pool = yaml.safe_load(yaml.safe_dump(POOL))
    pool["providers"] += [{"id": "scout", "kind": "fake", "family": "a", "reply": LITERATURE_OK,
                           "json_reply": {"queries": ["edge inference", "plant disease"]}},
                          {"id": "planner", "kind": "fake", "family": "a", "reply": PLAN_OK}]
    pool["roles"].update(literature_scout="scout", experiment_planner="planner")
    pool["library"] = {"openalex": False}
    return pool


# --- bibtex -----------------------------------------------------------------------------------
def test_bibtex_parse_and_keys():
    entries = parse(BIB)
    assert [e.key for e in entries] == ["smith2020deep", "ivanov2021plant"]
    smith = entries[0]
    assert smith.title == "Deep Learning on the Edge: A Survey" and smith.year == "2020"
    assert smith.fields["journal"] == "Journal of Edge Computing" and smith.doi == "10.1000/edge.2020.1"
    assert normalize_doi("https://dx.doi.org/10.1/X") == "10.1/x"
    assert make_key("Smith, John and Doe, Jane", "2020", "The Deep Survey", set()) == "smith2020deep"
    assert make_key("Иванов, Пётр", "2021", "Распознавание болезней", {"ivanov2021raspoznavanie"}) == "ivanov2021raspoznavanieb"
    assert entries[1].dump().startswith("@inproceedings{ivanov2021plant,")  # исходный текст сохраняется


def test_bibtex_edge_cases():
    text = """% комментарий сверху
@string{jx = "Journal X"}
@article{a1, title = "Deep {"}Learning{"} Edge", journal = jx,
  note = {see also @inproceedings{phantom, title={x}} for details}}

@misc{a2, title={Second}}
"""
    entries = parse(text)
    assert [e.key for e in entries] == ["a1", "a2"]  # @string пропущен, phantom внутри note — не запись
    assert entries[0].fields["title"] == 'Deep "Learning" Edge' and entries[0].fields["journal"] == "jx"
    assert "phantom" in entries[0].fields["note"]


def test_library_remove_keeps_comments_and_strings(tmp_path):
    lib = Library(tmp_path / "library")
    lib.root.mkdir()
    lib.bib_path.write_text("% шапка\n@string{jx = \"J\"}\n\n@article{a1, title={A}, journal=jx}\n\n@misc{a2, title={B}}\n",
                            encoding="utf-8")
    lib.reindex()
    lib.remove("a2")
    text = lib.bib_path.read_text(encoding="utf-8")
    assert text.startswith("% шапка\n@string{jx") and "@article{a1" in text and "a2" not in text
    assert lib.keys() == {"a1"}
    with pytest.raises(LibraryError, match="записи nope нет"):
        lib.remove("nope")


def test_openalex_to_entry_mapping():
    work = openalex.work_summary({"id": "https://openalex.org/W1", "doi": "https://doi.org/10.5/ab", "title": "Edge Plants",
                                  "publication_year": 2024, "type": "conference-paper",
                                  "authorships": [{"author": {"display_name": "Ann Lee"}}, {"author": {"display_name": "Bo Li"}}],
                                  "primary_location": {"source": {"display_name": "Proc. X"}}})
    e = openalex.to_entry(work, set())
    assert e.key == "lee2024edge" and e.type == "inproceedings"
    assert e.fields["author"] == "Ann Lee and Bo Li" and e.fields["booktitle"] == "Proc. X" and e.fields["doi"] == "10.5/ab"


# --- library ------------------------------------------------------------------------------------
def test_library_import_dedup_search_reindex(tmp_path):
    lib = Library(tmp_path / "library")
    bib = tmp_path / "in.bib"
    bib.write_text(BIB, encoding="utf-8")
    assert lib.import_path(bib)["added"] == ["smith2020deep", "ivanov2021plant"]
    dup = parse("@misc{other, title={Other}, doi={10.1000/edge.2020.1}}\n@misc{smith2020deep, title={X}}")
    assert lib.add_entries(dup) == {"added": [], "skipped": ["other", "smith2020deep"]}  # по DOI и по ключу
    assert lib.keys() == {"smith2020deep", "ivanov2021plant"}
    hits = lib.search("edge survey")
    assert hits and hits[0]["key"] == "smith2020deep"
    assert {h["key"] for h in lib.search("растений edge")} == {"ivanov2021plant", "smith2020deep"}  # AND пуст → OR
    assert lib.search("нет такого слова") == [] and lib.search("") == []
    (lib.root / "index.sqlite").unlink()
    assert lib.reindex() == 2 and len(lib.records()) == 2
    lib.remove("ivanov2021plant")
    assert lib.keys() == {"smith2020deep"} and len(lib.records()) == 1
    with pytest.raises(LibraryError, match="поддерживаются"):
        lib.import_path(tmp_path / "x.txt")


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="нужен pdftotext (poppler)")
def test_library_pdf_match_by_doi_title_and_stub(tmp_path):
    lib = Library(tmp_path / "library")
    lib.add_entries(parse(BIB))
    # заголовок чужой работы в середине текста не притягивает PDF к ней
    citing = tmp_path / "citing.pdf"
    citing.write_bytes(pdf_bytes("Own Title Here. " + "filler " * 300 + "as in Deep Learning on the Edge: A Survey"))
    assert not lib.import_pdf(citing)["matched"]
    by_doi = tmp_path / "paper one.pdf"
    by_doi.write_bytes(pdf_bytes("Some header. doi:10.1000/edge.2020.1 Introduction follows."))
    res = lib.import_pdf(by_doi)
    assert res["matched"] and res["added"] == ["smith2020deep"] and res["pdf"] == "paper_one.pdf"
    assert lib.search("introduction follows")[0]["key"] == "smith2020deep"
    rec = next(r for r in lib.records() if r["key"] == "smith2020deep")
    assert rec["pdf"] == "paper_one.pdf" and rec["has_text"]

    orphan = tmp_path / "unknown-2023.pdf"
    orphan.write_bytes(pdf_bytes("Quantization at the edge. doi 10.9999/quant.2023 abstract text"))
    res = lib.import_pdf(orphan)
    assert not res["matched"] and res["added"] == ["unknown2023"]
    stub = next(e for e in lib.entries() if e.key == "unknown2023")
    assert stub.type == "misc" and stub.doi == "10.9999/quant.2023" and is_stub(stub)
    # настоящая запись с тем же DOI вытесняет заглушку, текст PDF переходит к ней
    lib.add_entries(parse("@article{lee2023quant, title={Quantization at the edge}, author={Lee, A}, year={2023}, "
                          "doi={10.9999/quant.2023}}"))
    assert lib.keys() == {"smith2020deep", "ivanov2021plant", "lee2023quant", "citing"}
    assert lib.citable_keys() == {"smith2020deep", "ivanov2021plant", "lee2023quant"}  # заглушка citing не цитируема
    assert lib.search("abstract text")[0]["key"] == "lee2023quant"
    assert lib.reindex() == 4 and next(r for r in lib.records() if r["key"] == "lee2023quant")["pdf"] == "unknown-2023.pdf"


def test_suggestions_accept_reject(tmp_path):
    lib = Library(tmp_path / "library")
    works = [openalex.work_summary({"id": "W1", "doi": "https://doi.org/10.5/ab", "title": "Edge Plants",
                                    "publication_year": 2024, "type": "article",
                                    "authorships": [{"author": {"display_name": "Ann Lee"}}]}),
             openalex.work_summary({"id": "W2", "title": "No DOI work", "publication_year": 2020, "type": "book"})]
    assert lib.add_suggestions(works, "edge plants", "demo") == 2
    assert lib.add_suggestions(works, "edge plants", "demo") == 0  # повторы не копятся
    s1, s2 = sorted(lib.suggestions(), key=lambda s: s["id"])
    assert lib.resolve_suggestion(s1["id"], True) == "lee2024edge"
    assert lib.resolve_suggestion(s2["id"], False) is None
    assert lib.keys() == {"lee2024edge"} and lib.suggestions() == [] and len(lib.suggestions("accepted")) == 1
    assert lib.fetch_openalex("x", None, {"openalex": False}) == 0
    with pytest.raises(LibraryError, match="предложения 999 нет"):
        lib.resolve_suggestion(999, True)


# --- проверки -----------------------------------------------------------------------------------
def test_citation_checks(tmp_path):
    ws, project = make(tmp_path, lib_pool())
    Library(ws.root / "library").add_entries(parse(BIB))
    assert citation_keys("см. [@smith2020deep; @ivanov2021plant, p. 5] и @smith2020deep, но не mail@host.ru, "
                         "не https://x.com/@org/repo и не `@code`") == {"smith2020deep": 1, "ivanov2021plant": 1}
    rubric = ws.text("rubrics/literature.md")
    assert deterministic_checks(project, Artifact(type="literature", body=LITERATURE_OK), rubric) == []
    bad = LITERATURE_OK.replace("[@ivanov2021plant]", "[@lee2023fake]")
    issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="literature", body=bad), rubric)]
    assert len(issues) == 1 and "lee2023fake" in issues[0] and "не импортированная" in issues[0]
    stub_lib = Library(ws.root / "library")
    stub_lib.add_entries(parse("@misc{stub1, title={x.pdf}, wwp={stub}}"))
    issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="literature",
                                                        body=LITERATURE_OK.replace("[@smith2020deep]", "[@stub1]")), rubric)]
    assert len(issues) == 1 and "заглушка" in issues[0]
    no_cites = "\n".join(ln for ln in LITERATURE_OK.splitlines() if ln.startswith("#"))
    issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="literature", body=no_cites), rubric)]
    assert any("ни одной ссылки" in i for i in issues)
    plan_rubric = ws.text("rubrics/experiment_plan.md")
    assert deterministic_checks(project, Artifact(type="experiment_plan", body=PLAN_OK), plan_rubric) == []
    for weak in (PLAN_OK.replace("`", ""), PLAN_OK.replace("`latency_p50_ms`", "`fp32`").replace("`f1_zero_shot`", "`pandoc`")):
        issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="experiment_plan", body=weak), plan_rubric)]
        assert any("ключ реестра" in i for i in issues)
    draft_bad = "## Введение\n\nКак показано в [@nobody2000], всё хорошо.\n"  # черновик тоже проверяется на ключи
    issues = [b["issue"] for b in deterministic_checks(project, Artifact(type="draft", body=draft_bad),
                                                        ws.text("rubrics/draft.md"))]
    assert any("nobody2000" in i for i in issues)
    ctx = _context(project, Artifact(type="literature", body=LITERATURE_OK))  # судья видит процитированные записи
    assert "[@smith2020deep] Deep Learning on the Edge" in ctx and "[@stub1]" not in ctx
    ctx = _context(project, Artifact(type="draft", body="## Введение\n\nТекст.\n"))
    assert "Ещё не написаны: Постановка задачи" in ctx and "Введение," not in ctx


# --- стадии -------------------------------------------------------------------------------------
def test_stage_literature_then_experiment_plan(tmp_path):
    ws, project = make(tmp_path, lib_pool())
    Library(ws.root / "library").add_entries(parse(BIB))
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"},
                           source="Мы ускорили инференс на одноплатном компьютере.")
    project.decide("novelty", "approve")

    res = run_task(ws, enqueue(ws, "demo", "literature", {"input": "смотри работы по квантованию"}))
    assert res["error"] is None and res["accepted"], res
    assert res["queries"] == ["edge inference", "plant disease"] and res["openalex_suggested"] == 0
    art = project.artifact("literature")
    assert art.status == "in_review" and art.meta["created_by"]["role"] == "literature_scout"
    assert project.stage_state("literature") == "in_review"
    calls = ws.db().execute("SELECT role, count(*) FROM calls WHERE project='demo' GROUP BY role ORDER BY role").fetchall()
    assert {r[0]: r[1] for r in calls} == {"critic": 1, "judge": 2, "literature_scout": 3}  # запросы + черновик + ответ
    project.decide("literature", "approve")

    # выдуманный ключ: checker блокирует, автоправки не спасают, задача всё равно done
    set_reply(ws, "scout", LITERATURE_OK.replace("[@ivanov2021plant]", "[@fake2099]"))
    res = run_task(ws, enqueue(ws, "demo", "literature", {}, gate=False))
    assert res["error"] is None and not res["accepted"]
    assert all("fake2099" in b["issue"] for b in res["blockers"]) and project.artifact("literature").version == 4

    project.decide("literature", "approve")
    res = run_task(ws, enqueue(ws, "demo", "experiment_plan", {"input": "одна плата, 500 снимков"}))
    assert res["error"] is None and res["accepted"], res
    plan = project.artifact("experiment_plan")
    assert plan.meta["created_by"]["role"] == "experiment_planner" and "`latency_p50_ms`" in plan.body
    assert project.stage_state("experiment_plan") == "in_review"
    assert enqueue(ws, "demo", "draft", {"section": "Метод"}) > 0  # необязательные стадии не держат гейт


def test_stage_literature_requires_novelty_and_falls_back_without_queries(tmp_path):
    pool = lib_pool()
    next(p for p in pool["providers"] if p["id"] == "scout").pop("json_reply")  # fake отдаёт {"queries": []}
    ws, project = make(tmp_path, pool)
    Library(ws.root / "library").add_entries(parse(BIB))
    res = run_task(ws, enqueue(ws, "demo", "literature", {}, gate=False))
    assert "нет артефакта novelty" in res["error"]
    project.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"},
                           source="Edge inference on single-board computers")
    res = run_task(ws, enqueue(ws, "demo", "literature", {}, gate=False))
    assert res["error"] is None and res["queries"] == ["Edge inference on single-board computers"]


# --- веб и CLI ----------------------------------------------------------------------------------
def test_web_library_page(tmp_path):
    from fastapi.testclient import TestClient

    from workflow_writing_paper.web import create_app

    ws, _ = make(tmp_path, lib_pool())
    client = TestClient(create_app(ws, worker=False))
    assert "Импортируйте" in client.get("/library").text
    r = client.post("/library/import", files=[("files", ("refs.bib", BIB.encode(), "text/plain"))], follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/library?q=edge+survey").text
    assert "smith2020deep" in page and "ivanov2021plant" in page and "Найдено по" in page
    assert client.post("/library/import", data={}).status_code == 400
    assert client.post("/library/import", data={"path": str(tmp_path / "nope.txt")}).status_code == 400
    assert client.post("/library/remove/ivanov2021plant", follow_redirects=False).status_code == 303
    assert "ivanov2021plant" not in client.get("/library").text
    assert client.post("/library/remove/ivanov2021plant").status_code == 404
    assert client.post("/library/reindex", follow_redirects=False).status_code == 303
    assert client.post("/library/suggestions/5/accept").status_code == 404
    assert client.post("/library/suggestions/5/drop").status_code == 400
    assert "library/refs.bib" in client.get("/settings").text
    assert client.get("/settings/edit?rel=library/refs.bib").status_code == 200
    client.post("/projects", data={"slug": "p1", "template": "experimental", "journal": "generic", "language": "ru"})
    page = client.get("/projects/p1").text
    assert "появится в" not in page and "/run/literature" not in page  # все стадии реализованы; гейт держит literature
    p1 = Project.open(ws, "p1")
    p1.write_artifact("novelty", NOVELTY_OK, {"role": "ideator", "provider": "writer", "family": "a"})
    p1.decide("novelty", "approve")
    assert "/run/literature" in client.get("/projects/p1").text


def test_cli_lib(tmp_path, capsys):
    from workflow_writing_paper.cli import main

    ws = Workspace(tmp_path / "ws")
    ws.init()
    bib = tmp_path / "a.bib"
    bib.write_text(BIB, encoding="utf-8")
    assert main(["--workspace", str(ws.root), "lib", "import", str(bib)]) == 0
    assert "smith2020deep" in capsys.readouterr().out
    assert main(["--workspace", str(ws.root), "lib", "search", "edge", "survey"]) == 0
    assert "[@smith2020deep]" in capsys.readouterr().out
    assert main(["--workspace", str(ws.root), "lib", "search", "ничегонет"]) == 1
    assert main(["--workspace", str(ws.root), "lib", "reindex"]) == 0
    assert main(["--workspace", str(ws.root), "lib", "import", str(tmp_path / "x.txt")]) == 2
