"""Проверка артефакта судьями других семейств плюс детерминированный checker."""

from __future__ import annotations

import json
import time
from typing import Any

import yaml

from ..core.artifact import Artifact
from ..core.project import Project
from ..providers import Provider, ProviderError, build_pool, call_logged, pick_judges, role_providers
from ..providers.decision import DecisionProvider
from .checks import deterministic_checks
from .rubric import judge_schema, rubric_criteria, rubric_threshold, rubric_weights, weighted_overall


class ReviewError(RuntimeError):
    pass


def verdict(overall: float | None, blockers: int, threshold: float) -> str:
    if overall is None:
        return "error"
    if overall >= threshold:
        return "accept" if blockers == 0 else "minor"
    return "major" if overall >= threshold - 2 else "reject"


def _context(project: Project, art: Artifact) -> str:
    parts = [f"## Профиль журнала\n{yaml.safe_dump(project.journal(), allow_unicode=True)}"]
    if art.type in ("draft", "paper"):
        parts.append("## Реестр результатов (единственный источник чисел)\n"
                     + json.dumps(project.registry_data(), ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


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
    blockers = [b for b in (data.get("blockers") or []) if isinstance(b, dict)]
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
    cfg = project.ws.config()
    pool = build_pool(cfg)
    settings = project.manifest.get("review") or {}
    n = int(settings.get("judges", 2))
    rubric = project.ws.text(f"rubrics/{type_}.md")
    weights = rubric_weights(rubric)
    threshold = rubric_threshold(rubric, float(settings.get("threshold", 7.0)))
    author_family = str((art.meta.get("created_by") or {}).get("family", ""))
    judges = pick_judges(pool, role_providers(cfg, "judge"), author_family, n)
    if len(judges) < n:
        raise ReviewError(f"нужно {n} судей из семейств, отличных от {author_family!r}; доступно {len(judges)}")

    reports: dict[str, dict[str, Any]] = {}
    for judge in judges:
        if isinstance(judge, DecisionProvider):
            rep = _decision_judge(judge, project, art, rubric, weights, threshold)
        else:
            rep = _text_judge(judge, project, art, rubric, weights, threshold, task)
        reports[judge.id] = {"judge": {"provider": judge.id, "family": judge.family}, **rep}

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
    all_blockers = [{**b, "judge": jid} for jid, r in reports.items() for b in r["blockers"]]
    errors = [f"{jid}: {r['error']}" for jid, r in reports.items() if r["error"]]
    accepted = len(overalls) >= n and min(overalls) >= threshold and not all_blockers

    art.status = "in_review"
    art.meta["reviews"] = paths
    art.meta["review_summary"] = {"accepted": accepted, "threshold": threshold,
                                  "min_overall": min(overalls) if overalls else None,
                                  "blockers": len(all_blockers), "errors": errors}
    project.update_artifact(art)
    return {"accepted": accepted, "threshold": threshold, "min_overall": min(overalls) if overalls else None,
            "blockers": all_blockers, "errors": errors, "reports": reports}


__all__ = ["ReviewError", "review_artifact", "verdict"]
