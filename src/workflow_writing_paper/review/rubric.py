"""Рубрика артефакта: критерии, веса, порог; схема ответа судьи; взвешенная оценка; доля языка."""

from __future__ import annotations

import re
from typing import Any

_RUBRIC_ROW = re.compile(r"^\|\s*(C\d+)\s*\|(.*)\|\s*([\d.]+)\s*\|\s*$", re.MULTILINE)
_THRESHOLD = re.compile(r"Порог принятия:\s*([\d.]+)|Acceptance threshold:\s*([\d.]+)")
_CYR = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT = re.compile(r"[a-z]", re.IGNORECASE)

LANGUAGE_MIN_SHARE = 0.8  # меньшая доля букв языка задачи — текст не на том языке


def rubric_criteria(rubric: str) -> dict[str, str]:
    """Коды и формулировки критериев из таблицы рубрики: строки вида `| C1 | критерий | 0.4 |`."""
    return {code: text.strip() for code, text, _ in _RUBRIC_ROW.findall(rubric)}


def rubric_weights(rubric: str) -> dict[str, float]:
    weights = {code: float(w) for code, _, w in _RUBRIC_ROW.findall(rubric)}
    if not weights:
        raise ValueError("в рубрике нет строк критериев вида | C1 | … | вес |")
    return weights


def rubric_threshold(rubric: str, default: float) -> float:
    m = _THRESHOLD.search(rubric)
    return float(m.group(1) or m.group(2)) if m else default


def judge_schema(weights: dict[str, float], with_blockers: bool = False) -> dict[str, Any]:
    """Целое 0–10 по каждому критерию; итог судья не ставит — его считает код по весам."""
    crit = {"type": "integer", "minimum": 0, "maximum": 10}
    props: dict[str, Any] = {
        "scores": {"type": "object", "properties": dict.fromkeys(weights, crit), "required": list(weights)},
        "comments": {"type": "array", "items": {"type": "string"}},
    }
    if with_blockers:
        props["blockers"] = {"type": "array", "items": {
            "type": "object", "properties": {"quote": {"type": "string"}, "issue": {"type": "string"},
                                             "fix": {"type": "string"}},
            "required": ["quote", "issue"]}}
    return {"type": "object", "properties": props, "required": list(props)}


def weighted_overall(scores: Any, weights: dict[str, float]) -> float | None:
    """Взвешенная оценка 0–10; None, если критерия нет или оценка вне шкалы."""
    if not isinstance(scores, dict):
        return None
    vals: dict[str, float] = {}
    for k in weights:
        v = scores.get(k)
        if not isinstance(v, (int, float)) or not 0 <= v <= 10:
            return None
        vals[k] = float(v)
    return round(sum(vals[k] * w for k, w in weights.items()) / sum(weights.values()), 2)


def language_share(text: str, language: str) -> float:
    """Доля букв языка среди кириллицы и латиницы; 1.0 — текст целиком на этом языке."""
    cyr, lat = len(_CYR.findall(text)), len(_LAT.findall(text))
    if cyr + lat == 0:
        return 1.0
    return (cyr if language == "ru" else lat) / (cyr + lat)
