"""Стадия collect: тема → запросы → OpenAlex → отбор моделью по аннотациям → снежный ком → библиотека проекта.

Итог — артефакт `corpus` (список собранных работ с оценками) на одно подтверждение автора: одобрить корпус
целиком или вычеркнуть лишнее. Записи приходят из OpenAlex с идентификатором и DOI, модель их только оценивает,
поэтому выдумать работу она не может.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.project import Project
from ..library import openalex
from ..library.bibtex import Entry

logger = logging.getLogger(__name__)

DEFAULTS = {"queries": 6, "per_query": 15, "min_score": 6, "max_works": 40, "snowball": 10, "pdf": True,
            "max_pdf_mb": 30}
BATCH = 10  # работ на один вызов отбора: 15 с пояснениями не влезали в ответ gemma4:12b
SCREEN_TOKENS = 4000
PDF_TRIES = 3  # адресов PDF на работу: лучший открытый, затем копии в репозиториях
QUERIES_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
                  "required": ["queries"]}
SCORES_SCHEMA = {"type": "object", "required": ["scores"], "properties": {"scores": {"type": "array", "items": {
    "type": "object", "required": ["id", "score"],
    "properties": {"id": {"type": "string"}, "score": {"type": "integer"}, "reason": {"type": "string"}}}}}}
_NET_ERRORS = (OSError, ValueError, KeyError, TypeError)  # сеть, таймаут, формат ответа OpenAlex


def settings(project: Project) -> dict[str, Any]:
    """Параметры сбора: DEFAULTS ← `collect:` манифеста проекта."""
    return {**DEFAULTS, **(project.manifest.get("collect") or {})}


def _short(openalex_id: str) -> str:
    return openalex_id.rsplit("/", 1)[-1]


def _search(queries: list[str], per_query: int, mailto: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for q in queries:
        try:
            works = openalex.search(q, limit=per_query, mailto=mailto)
        except _NET_ERRORS as e:  # запрос пропускается, остальные продолжаются
            logger.warning("OpenAlex «%s»: %s", q, e)
            continue
        for w in works:
            if w["id"] and not w["retracted"]:
                found.setdefault(w["id"], w)
    return found


def _screen(project: Project, topic: str, works: list[dict[str, Any]], task: str | None) -> int:
    """Оценка релевантности 0–10 с причиной, пишется в словари работ. Возвращает число неоценённых (им 0)."""
    unscored = 0
    for i in range(0, len(works), BATCH):
        unscored += _screen_batch(project, topic, works[i:i + BATCH], task)
    return unscored


def _screen_batch(project: Project, topic: str, batch: list[dict[str, Any]], task: str | None) -> int:
    """Одна пачка; если модель не вернула JSON (обрыв по длине и т. п.) — две половины по отдельности."""
    from . import StageError, call_role  # поздний импорт: roles/__init__ импортирует этот модуль

    listing = "\n".join(f"- id {_short(w['id'])} | {w['title']} ({w['year']}; {w['venue']}; "
                        f"цитирований {w['cited_by']})\n  {(w['abstract'] or 'аннотации нет')[:1200]}" for w in batch)
    try:
        comp, _ = call_role(project, "literature_scout",
                            f"## Тема исследования\n{topic}\n\n## Найденные работы\n{listing}\n\n"
                            "Оцени релевантность каждой работы теме от 0 до 10 по названию и аннотации: 9–10 — прямо про "
                            "тему, 6–8 — близкий метод или задача, 3–5 — смежная область, 0–2 — не про тему. Без "
                            "аннотации оценивай осторожно. Ответ — JSON {\"scores\": [{\"id\", \"score\", \"reason\"}]}, "
                            "reason — не длиннее десяти слов.", task, SCREEN_TOKENS, json_schema=SCORES_SCHEMA)
    except StageError as e:
        if len(batch) > 1:
            half = len(batch) // 2
            return (_screen_batch(project, topic, batch[:half], task)
                    + _screen_batch(project, topic, batch[half:], task))
        logger.warning("отбор %s: %s", batch[0]["id"], e)
        batch[0].update(score=0, reason="модель не оценила")
        return 1
    scores = {_short(str(s.get("id", ""))): s for s in (comp.data or {}).get("scores", []) if isinstance(s, dict)}
    unscored = 0
    for w in batch:
        s = scores.get(_short(w["id"])) or {}
        try:
            w["score"] = max(0, min(10, int(s["score"])))
        except (KeyError, TypeError, ValueError):
            w["score"], unscored = 0, unscored + 1
        w["reason"] = str(s.get("reason") or ("" if "score" in s else "модель не оценила"))
    return unscored


def _select(works: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    good = [w for w in works.values() if w.get("score", 0) >= int(cfg["min_score"])]
    return sorted(good, key=lambda w: (-w["score"], -w["cited_by"]))[:int(cfg["max_works"])]


def corpus_body(meta: dict[str, Any]) -> str:
    """Тело артефакта corpus из его заголовка: тема, запросы, таблица работ, отсеянное и вычеркнутое."""
    works = list(meta.get("works") or [])
    rows = "\n".join(f"| [@{w['key']}] | {w['title']} | {w['year']} | {w['cited_by']} | {w['score']} | "
                     f"{'PDF' if w['pdf'] else 'аннотация' if w['abstract'] else '—'} | {w['reason']} |" for w in works)
    excluded = list(meta.get("excluded") or [])
    return (f"## Тема\n{meta.get('source') or ''}\n\n## Запросы\n"
            + "\n".join(f"- {q}" for q in meta.get("queries") or [])
            + f"\n\n## Корпус ({len(works)} работ, полных текстов {sum(1 for w in works if w['pdf'])})\n"
            "| ключ | работа | год | цитирований | оценка | текст | почему |\n|---|---|---|---|---|---|---|\n" + rows
            + f"\n\n## Отсеяно\n{meta.get('dropped', 0)} работ с оценкой ниже {meta.get('min_score')} или сверх лимита."
            + (f"\nНе оценено моделью (оценка 0): {meta['unscored']}." if meta.get("unscored") else "")
            + (f"\nВычеркнуто автором: {', '.join(excluded)}." if excluded else "") + "\n")


def _add_to_library(project: Project, top: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    """Записи → refs.bib проекта, открытые PDF → pdf/; в словари работ пишутся `key` и `pdf`."""
    lib = project.library()
    existing = {e.fields.get("url", ""): e.key for e in lib.entries()}
    with_text = {r["key"] for r in lib.records() if r["has_text"]}
    taken = lib.keys()
    entries: list[Entry] = []
    for w in top:
        if w["id"] in existing:  # уже в библиотеке с прошлого сбора
            w["key"], w["pdf"] = existing[w["id"]], existing[w["id"]] in with_text
            continue
        entry = openalex.to_entry(w, taken)
        taken.add(entry.key)
        w["key"], w["pdf"] = entry.key, False
        dst = lib.pdf_dir / f"{entry.key}.pdf"
        for url in w["pdf_urls"][:PDF_TRIES] if cfg["pdf"] else []:  # издатель часто отдаёт HTML — пробуем копии
            lib.pdf_dir.mkdir(parents=True, exist_ok=True)
            if openalex.download_pdf(url, dst, int(cfg["max_pdf_mb"]) * 2**20):
                entry.fields["file"] = f"pdf/{dst.name}"
                break
        entries.append(entry)
    lib.add_entries(entries)
    by_key = {w["key"]: w for w in top}
    for e in entries:
        if e.fields.get("file"):
            by_key[e.key]["pdf"] = lib.attach_pdf(e.key, lib.root / e.fields["file"])


def stage_collect(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    from . import StageError, call_role

    prev = project.artifact("corpus")
    topic = str(payload.get("input") or (prev.meta.get("source") if prev else "") or "").strip()
    if not topic:
        raise StageError("нужна тема исследования (payload.input): абзац о том, что изучаем")
    cfg = settings(project)
    mailto = str((project.ws.config().get("library") or {}).get("mailto") or "")

    comp, provider = call_role(project, "literature_scout", f"## Тема исследования\n{topic}\n\nСформулируй "
                               f"{cfg['queries']} поисковых запросов по 2–6 слов для поиска научных работ в OpenAlex: "
                               "основная масса на английском, разные стороны темы (задача, методы, данные, оценка). "
                               "Ответ — JSON {\"queries\": [...]}.", task, 800, json_schema=QUERIES_SCHEMA)
    queries = [str(q).strip() for q in (comp.data or {}).get("queries", []) if str(q).strip()][:int(cfg["queries"])]
    queries = queries or [topic[:120]]

    found = _search(queries, int(cfg["per_query"]), mailto)
    if not found:
        raise StageError("OpenAlex ничего не вернул: проверьте сеть или переформулируйте тему")
    unscored = _screen(project, topic, list(found.values()), task)
    top = _select(found, cfg)

    refs = list(dict.fromkeys(r for w in top[:int(cfg["snowball"])] for r in w["refs"] if r not in found))
    if refs:  # снежный ком: один уровень по спискам литературы лучших работ
        try:
            extra = {w["id"]: w for w in openalex.by_ids(refs[:openalex.MAX_IDS], mailto=mailto)
                     if w["id"] and not w["retracted"] and w["id"] not in found}
        except _NET_ERRORS as e:
            logger.warning("OpenAlex по ссылкам: %s", e)
            extra = {}
        unscored += _screen(project, topic, list(extra.values()), task)
        found.update(extra)
        top = _select(found, cfg)

    _add_to_library(project, top, cfg)
    works = [{**{k: w[k] for k in ("key", "id", "title", "year", "cited_by", "score", "reason", "pdf")},
              "abstract": bool(w["abstract"])} for w in top]  # в заголовок — только факт наличия аннотации
    meta = {"source": topic, "queries": queries, "works": works, "dropped": len(found) - len(top),
            "min_score": int(cfg["min_score"]), "excluded": [], "unscored": unscored}
    art = project.write_artifact("corpus", corpus_body(meta), {"role": "literature_scout", "provider": provider.id,
                                                               "family": provider.family, "call": comp.log_path},
                                 status="in_review", source=topic)
    art.meta.update({k: v for k, v in meta.items() if k != "source"})
    project.update_artifact(art)
    return {"accepted": False, "blockers": [], "queries": queries, "found": len(found), "selected": len(works),
            "pdf": sum(1 for w in works if w["pdf"]), "unscored": unscored}


def exclude(project: Project, key: str) -> None:
    """Вычеркнуть работу из неодобренного корпуса: запись и PDF уходят из библиотеки проекта."""
    art = project.artifact("corpus")
    if art is None or art.status == "approved":
        raise ValueError("вычёркивать можно только из неодобренного корпуса")
    works = list(art.meta.get("works") or [])
    if key not in {w.get("key") for w in works}:
        raise ValueError(f"работы {key} в корпусе нет")
    lib = project.library()
    (lib.pdf_dir / f"{key}.pdf").unlink(missing_ok=True)
    if any(e.key == key for e in lib.entries()):
        lib.remove(key)
    art.meta["works"] = [w for w in works if w.get("key") != key]
    art.meta["excluded"] = [*(art.meta.get("excluded") or []), key]
    art.body = corpus_body(art.meta)
    project.update_artifact(art)


__all__ = ["DEFAULTS", "corpus_body", "exclude", "settings", "stage_collect"]
