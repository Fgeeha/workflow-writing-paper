"""Стенд: каждая модель решает эталонные задачи роли, ответы оценивают судьи других семейств."""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..core import Workspace
from ..providers import Provider, ProviderError, build_pool, call_logged, pick_judges, role_providers
from ..providers.decision import DecisionProvider
from ..review.rubric import (
    LANGUAGE_MIN_SHARE,
    judge_schema,
    language_share,
    rubric_criteria,
    rubric_weights,
    weighted_overall,
)

logger = logging.getLogger(__name__)


_WORD = re.compile(r"\w+", re.UNICODE)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(text.lower()) if len(w) > 3}


def coverage(answer: str, expected: dict[str, Any] | None) -> float | None:
    """Доля пунктов must_mention, все значимые слова которых (по первым 5 буквам) есть в ответе.

    ponytail: грубое сопоставление основ; если начнёт врать на синонимах — отдать пункты судье.
    """
    items = (expected or {}).get("must_mention") or []
    if not items:
        return None
    have = _stems(answer)
    return sum(1 for item in items if _stems(item) <= have) / len(items)


def load_tasks(ws: Workspace, role: str) -> list[dict[str, Any]]:
    folder = ws.root / "eval" / "tasks" / role
    tasks = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.yaml"))]
    return [t for t in tasks if t and t.get("role", role) == role]


def _judge_messages(ws: Workspace, task: dict[str, Any], rubric: str, answer: str) -> list[dict[str, str]]:
    lang = task.get("language", "ru")
    expected = yaml.safe_dump(task.get("expected") or {}, allow_unicode=True)
    user = (f"## Рубрика\n{rubric}\n\n## Задача, поставленная модели\n{task['input']}\n\n"
            f"## Чего ждёт автор от хорошего ответа\n{expected}\n\n## Ответ модели\n{answer}")
    return [{"role": "system", "content": ws.text(f"prompts/{lang}/judge.md")}, {"role": "user", "content": user}]


def run_bench(ws: Workspace, role: str, provider_ids: list[str], n_judges: int = 2,
              progress: Callable[[int, int, str], None] | None = None) -> str:
    """Прогнать роль по всем задачам на выбранных провайдерах. Возвращает идентификатор прогона.

    ponytail: последовательно; облачные модели можно гонять параллельно, если стенд станет медленным.
    """
    cfg = ws.config()
    pool = build_pool(cfg)
    unknown = [p for p in provider_ids if p not in pool]
    if unknown:
        raise ValueError(f"нет в пуле (или выключены): {', '.join(unknown)}")
    tasks = load_tasks(ws, role)
    if not tasks:
        raise ValueError(f"нет задач в {ws.root / 'eval' / 'tasks' / role}")
    judge_ids = role_providers(cfg, "judge")
    run = time.strftime("%Y%m%d-%H%M%S")
    run_dir = ws.root / "eval" / "runs" / run
    run_dir.mkdir(parents=True, exist_ok=True)
    db = ws.db()
    total, done = len(tasks) * len(provider_ids), 0
    try:
        for pid in provider_ids:
            for task in tasks:
                if progress:
                    progress(done, total, f"{pid} · {task['id']}")
                _run_one(ws, db, run, run_dir, role, task, pool[pid], pool, judge_ids, n_judges)
                done += 1
    finally:
        db.close()
    if progress:
        progress(done, total, "готово")
    return run


def _run_one(ws: Workspace, db: Any, run: str, run_dir: Path, role: str, task: dict[str, Any],
             author: Provider, pool: dict[str, Provider], judge_ids: list[str], n_judges: int) -> None:
    lang = task.get("language", "ru")
    log_dir = run_dir / "calls"
    messages = [{"role": "system", "content": ws.text(f"prompts/{lang}/{role}.md")},
                {"role": "user", "content": task["input"]}]
    row: dict[str, Any] = {"run": run, "role": role, "task": task["id"], "provider": author.id,
                           "family": author.family}
    try:
        comp = call_logged(author, messages, role=role, log_dir=log_dir, db=db, task=task["id"],
                           max_tokens=int(task.get("max_tokens", 2048)), seed=42)
    except ProviderError as e:
        logger.warning("bench %s/%s: %s", author.id, task["id"], e)
        _insert(db, {**row, "error": str(e)})
        return
    answer_path = run_dir / f"{task['id']}.{author.id}.md"
    answer_path.write_text(comp.text, encoding="utf-8")
    rubric = ws.text(task.get("rubric") or f"rubrics/{role}.md")
    weights = rubric_weights(rubric)
    scores: dict[str, Any] = {}
    for judge in pick_judges(pool, judge_ids, author.family, n_judges):
        if isinstance(judge, DecisionProvider):
            scores[judge.id] = _decision_judge(judge, task, rubric, comp.text, weights, log_dir, db)
            continue
        try:
            jc = call_logged(judge, _judge_messages(ws, task, rubric, comp.text), role="judge", log_dir=log_dir,
                             db=db, task=task["id"], json_schema=judge_schema(weights), max_tokens=1500,
                             temperature=0.0, seed=42)
        except ProviderError as e:
            scores[judge.id] = {"family": judge.family, "error": str(e)}
            continue
        overall = weighted_overall(jc.data.get("scores"), weights)
        scores[judge.id] = {"family": judge.family, **jc.data, "overall": overall}
        if overall is None:
            scores[judge.id]["error"] = "оценки критериев отсутствуют или вне шкалы 0–10"
    overalls = [float(s["overall"]) for s in scores.values() if isinstance(s.get("overall"), (int, float))]
    error = None if len(overalls) >= n_judges else f"оценок судей других семейств: {len(overalls)} из {n_judges}"
    share = language_share(comp.text, lang)
    if share < LANGUAGE_MIN_SHARE:
        error = f"ответ не на языке задачи ({lang}: {share:.0%})" + (f"; {error}" if error else "")
    _insert(db, {**row, "answer_path": str(answer_path), "judge_scores": json.dumps(scores, ensure_ascii=False),
                 "judge_overall": statistics.median(overalls) if overalls else None,
                 "coverage": coverage(comp.text, task.get("expected")), "duration_ms": comp.duration_ms,
                 "cost_usd": comp.cost_usd, "error": error})


def _decision_judge(judge: DecisionProvider, task: dict[str, Any], rubric: str, answer: str,
                    weights: dict[str, float], log_dir: Path, db: Any) -> dict[str, Any]:
    """Оценка decision-моделью: по вопросу `score` на критерий; запись в журнал вызовов как у текстовых судей."""
    expected = yaml.safe_dump(task.get("expected") or {}, allow_unicode=True)
    state = (f"## Задача, поставленная модели\n{task['input']}\n\n## Чего ждёт автор от хорошего ответа\n"
             f"{expected}\n\n## Ответ модели\n{answer}")
    criteria = rubric_criteria(rubric)
    started = time.monotonic()
    record: dict[str, Any] = {"provider": judge.id, "family": judge.family, "model": judge.model, "role": "judge",
                              "task": task["id"], "state": state, "criteria": criteria}
    try:
        result, tokens = judge.score(state, criteria)
    except ProviderError as e:
        record["error"] = str(e)
        _log_call(log_dir, db, judge, task["id"], record, 0, started, str(e))
        return {"family": judge.family, "error": str(e)}
    scores = {code: v["score"] for code, v in result.items()}
    record.update(data=result, tokens_in=tokens)
    _log_call(log_dir, db, judge, task["id"], record, tokens, started, None)
    return {"family": judge.family, "scores": scores, "probabilities": {c: v["probabilities"] for c, v in result.items()},
            "comments": [], "overall": weighted_overall(scores, weights)}


def _log_call(log_dir: Path, db: Any, judge: Provider, task_id: str, record: dict[str, Any], tokens: int,
              started: float, error: str | None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    duration = int((time.monotonic() - started) * 1000)
    path = log_dir / f"{time.strftime('%Y-%m-%dT%H-%M-%S')}-{time.monotonic_ns() % 1_000_000:06d}-judge-{judge.id}.json"
    path.write_text(json.dumps({**record, "duration_ms": duration}, ensure_ascii=False, indent=1, default=str),
                    encoding="utf-8")
    db.execute("INSERT INTO calls(task, role, provider, family, prompt_path, tokens_in, tokens_out, duration_ms,"
               " cost_usd, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (task_id, "judge", judge.id, judge.family, str(path), tokens, 0, duration, 0.0, error))
    db.commit()


def _insert(db: Any, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    db.execute(f"INSERT INTO bench_runs({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    db.commit()


def _median(values: list[Any]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def summary(db: Any, role: str, run: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Таблица роль × модель и рекомендация: лучшая по оценке автора, иначе по судьям; при равенстве — дешевле.

    `run` — идентификатор прогона; None — по всем прогонам роли.
    """
    sql, args = "SELECT * FROM bench_runs WHERE role = ?", [role]
    if run:
        sql, args = sql + " AND run = ?", [*args, run]
    by: dict[str, list[Any]] = {}
    for r in db.execute(sql, args).fetchall():
        by.setdefault(r["provider"], []).append(r)
    table = []
    for pid, rs in sorted(by.items()):
        ok = [r for r in rs if r["answer_path"] and not r["error"]]  # ошибка вызова, языка или нехватки судей
        cov = [r["coverage"] for r in ok if r["coverage"] is not None]
        table.append({
            "provider": pid, "family": rs[0]["family"], "n": len(ok), "errors": len(rs) - len(ok),
            "judge": _median([r["judge_overall"] for r in ok]),
            "human": _median([r["human_score"] for r in ok]),
            "coverage": sum(cov) / len(cov) if cov else None,
            "duration_ms": _median([r["duration_ms"] for r in ok]),
            "cost_usd": sum(r["cost_usd"] or 0 for r in ok) / len(ok) if ok else None,
        })
    has_human = any(t["human"] is not None for t in table)
    key = "human" if has_human else "judge"
    ranked = sorted((t for t in table if t[key] is not None), key=lambda t: (-t[key], t["cost_usd"] or 0))
    if not ranked:
        return table, None
    return table, f"{ranked[0]['provider']} (по оценке {'автора' if has_human else 'судей'})"
