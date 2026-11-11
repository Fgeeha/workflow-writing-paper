"""Проверка артефакта судьями других семейств плюс детерминированный checker."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.artifact import Artifact
from ..core.project import Project
from ..providers import Provider, ProviderError, build_pool, call_logged, pick_judges
from ..providers.decision import DecisionProvider
from .checks import CITED_TYPES, citation_keys, deterministic_checks
from .rubric import judge_schema, rubric_criteria, rubric_threshold, rubric_weights, weighted_overall


class ReviewError(RuntimeError):
    pass


def verdict(overall: float | None, n_blockers: int, threshold: float) -> str:
    """accept | minor | major | reject | error — по итоговой оценке и числу блокеров."""
    if overall is None:
        return "error"
    if overall >= threshold:
        return "accept" if n_blockers == 0 else "minor"
    return "major" if overall >= threshold - 2 else "reject"


def _context(project: Project, art: Artifact) -> str:
    parts = [project.context_header(with_registry=art.type in ("draft", "paper"))]
    if art.meta.get("source"):
        parts.append("## Материалы автора (исходный ввод; всё, что есть в нём, считается подтверждённым)\n"
                     + str(art.meta["source"]))
    if art.type == "draft":  # черновик пишется по разделу: ещё не написанные — не блокер
        todo = [s for s in project.sections() if f"## {s}" not in art.body]
        if todo:
            parts.append("## Черновик пишется по разделам\nЕщё не написаны: " + ", ".join(todo)
                         + ". Их отсутствие не блокер; оценивай написанные разделы.")
    if art.type in CITED_TYPES:  # судья видит, на что ссылается текст; существование ключей проверяет checker
        cited = citation_keys(art.body)
        rows = [f"- [@{e.key}] {e.fields.get('title', '')} ({e.fields.get('year', '')})"
                for e in project.library().entries() if e.key in cited]
        if rows:
            parts.append("## Процитированные записи библиотеки (ключи проверены автоматически)\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _locate(blockers: list[dict[str, Any]], body: str) -> list[dict[str, Any]]:
    """Проставить номер строки по цитате судьи, если он её не указал."""
    lines = body.splitlines()
    for b in blockers:
        quote = str(b.get("quote") or "").strip()
        if b.get("line") or not quote:
            continue
        needle = quote[:40].lower()
        b["line"] = next((i for i, ln in enumerate(lines, 1) if needle in ln.lower()), None)
    return blockers


def _line(b: dict[str, Any]) -> int:
    """Номер строки блокера; судья-модель может вернуть «5–7» или текст — тогда 0."""
    try:
        return int(b.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def agreed_first(blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Блокеры, которые на одной строке нашли хотя бы два судьи-модели, — с `agreed` и в начале списка.

    ponytail: совпадение — только по номеру строки; два разных замечания к одной строке тоже «совпадут».
    """
    judges_by_line: dict[int, set[str]] = {}
    for b in blockers:
        if _line(b) and b.get("judge") != "checker":
            judges_by_line.setdefault(_line(b), set()).add(str(b.get("judge")))
    out = [{**b, "agreed": len(judges_by_line.get(_line(b), ())) >= 2} for b in blockers]
    return sorted(out, key=lambda b: not b["agreed"])  # sorted устойчив: внутри групп порядок судей


def estimate_cost_usd(judges: list[Provider], prompt_chars: int, max_tokens: int) -> float:
    """Грубая оценка стоимости проверки по таблице цен: ~4 символа на токен входа плюс полный выход."""
    return sum(j.cost(prompt_chars // 4, max_tokens) for j in judges)


def _text_judge(judge: Provider, project: Project, art: Artifact, rubric: str, weights: dict[str, float],
                threshold: float, task: str | None) -> dict[str, Any]:
    user = (f"## Рубрика\n{rubric}\n\n{_context(project, art)}\n\n## Артефакт «{art.type}» v{art.version}\n{art.body}")
    messages = [{"role": "system", "content": project.ws.text(f"prompts/{project.language}/judge.md")},
                {"role": "user", "content": user}]
    db = project.ws.db()
    try:
        comp = call_logged(judge, messages, role="judge", log_dir=project.path / "calls", db=db,
                           project=project.slug, task=task, json_schema=judge_schema(weights, with_blockers=True),
                           max_tokens=2500, temperature=0.0, seed=42)
    except ProviderError as e:
        return {"error": str(e), "overall": None, "blockers": [], "comments": [], "scores": {}}
    finally:
        db.close()
    data = comp.data or {}
    overall = weighted_overall(data.get("scores"), weights)
    blockers = _locate([b for b in (data.get("blockers") or []) if isinstance(b, dict)], art.body)
    return {"scores": data.get("scores"), "overall": overall, "comments": data.get("comments") or [],
            "blockers": blockers, "verdict": verdict(overall, len(blockers), threshold),
            "error": None if overall is not None else "оценки критериев отсутствуют или вне шкалы"}


def _decision_judge(judge: DecisionProvider, project: Project, art: Artifact, rubric: str,
                    weights: dict[str, float], threshold: float) -> dict[str, Any]:
    state = f"{_context(project, art)}\n\n## Артефакт «{art.type}» v{art.version}\n{art.body}"
    try:
        result, _ = judge.score(state, rubric_criteria(rubric))
    except ProviderError as e:
        return {"error": str(e), "overall": None, "blockers": [], "comments": [], "scores": {}}
    scores = {c: v["score"] for c, v in result.items()}
    overall = weighted_overall(scores, weights)
    return {"scores": scores, "overall": overall, "comments": [], "blockers": [],
            "verdict": verdict(overall, 0, threshold), "error": None}


def review_artifact(project: Project, type_: str, task: str | None = None) -> dict[str, Any]:
    """Судьи других семейств + checker. Переводит артефакт в in_review, пишет отчёты в artifacts/reviews/."""
    art = project.artifact(type_)
    if art is None:
        raise ReviewError(f"артефакта {type_} нет")
    pool = build_pool(project.ws.config(), text=project.ws.text)
    settings = project.review_settings()
    n = int(settings["judges"])
    rubric = project.ws.text(f"rubrics/{type_}.md")
    weights = rubric_weights(rubric)
    # порог: манифест проекта → строка «Порог принятия» рубрики → 7.0
    threshold = float(settings["threshold"]) if settings["threshold"] is not None else rubric_threshold(rubric, 7.0)
    author_family = str((art.meta.get("created_by") or {}).get("family", ""))
    candidates = project.judge_candidates()
    eligible = pick_judges(pool, candidates, author_family, len(candidates))  # все подходящие, по приоритету
    if len(eligible) < n:
        raise ReviewError(f"нужно {n} судей из семейств, отличных от {author_family!r}; доступно {len(eligible)}")
    estimate = estimate_cost_usd(eligible[:n], len(rubric) + len(_context(project, art)) + len(art.body), 2500)
    ok, why = project.budget_left(extra_usd=estimate)
    if not ok:
        raise ReviewError(f"проверка остановлена: оценка стоимости ${estimate:.3f}; {why}")

    def judge_one(judge: Provider) -> dict[str, Any]:
        if isinstance(judge, DecisionProvider):
            return _decision_judge(judge, project, art, rubric, weights, threshold)
        return _text_judge(judge, project, art, rubric, weights, threshold, task)

    reports: dict[str, dict[str, Any]] = {}
    queue, succeeded = list(eligible), 0
    # раунды: недостающие судьи параллельно; отказавший записывается, его место занимает следующий.
    # GPU-модели всё равно идут по одной — семафор Provider._slot.
    while queue and succeeded < n:
        batch, queue = queue[:n - succeeded], queue[n - succeeded:]
        with ThreadPoolExecutor(len(batch)) as pool_:
            for judge, rep in zip(batch, pool_.map(judge_one, batch), strict=True):
                reports[judge.id] = {"judge": {"provider": judge.id, "family": judge.family}, **rep}
                succeeded += rep["overall"] is not None

    checker_blockers = deterministic_checks(project, art, rubric)
    reports["checker"] = {"judge": {"provider": "checker", "family": "code"}, "scores": {}, "overall": None,
                          "comments": [], "blockers": checker_blockers, "error": None,
                          "verdict": "accept" if not checker_blockers else "reject"}

    paths = []
    for jid, rep in reports.items():
        p = project.path / "artifacts" / "reviews" / f"{type_}.v{art.version}.{jid}.json"
        p.write_text(json.dumps({"artifact": f"{type_}.v{art.version}", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 **rep}, ensure_ascii=False, indent=1), encoding="utf-8")
        paths.append(str(p.relative_to(project.path)))

    overalls = [r["overall"] for r in reports.values() if r["overall"] is not None]
    all_blockers = agreed_first([{**b, "judge": jid} for jid, r in reports.items() for b in r["blockers"]])
    agreed = [b for b in all_blockers if b["agreed"]]
    errors = [f"{jid}: {r['error']}" for jid, r in reports.items() if r["error"]]
    accepted = len(overalls) >= n and min(overalls) >= threshold and not all_blockers

    art.status = "in_review"
    art.meta["reviews"] = paths
    art.meta["review_summary"] = {"accepted": accepted, "threshold": threshold,
                                  "min_overall": min(overalls) if overalls else None,
                                  "blockers": len(all_blockers), "errors": errors,
                                  "agreed": [{k: b.get(k) for k in ("line", "judge", "issue", "quote")} for b in agreed]}
    project.update_artifact(art)
    return {"accepted": accepted, "threshold": threshold, "min_overall": min(overalls) if overalls else None,
            "blockers": all_blockers, "errors": errors, "reports": reports}


__all__ = ["ReviewError", "agreed_first", "estimate_cost_usd", "review_artifact", "verdict"]
