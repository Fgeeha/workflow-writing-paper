"""Реестр результатов `results.json` — единственный источник чисел в тексте статьи."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_CODE_BLOCK = re.compile(r"```.*?```|`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$", re.DOTALL)
_CITATION = re.compile(r"\[[^\]\n]*\]|\([^()\n]*\d{4}[^()\n]*\)")  # [key, 2021], (Иванов, 2021)
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s")
_NUMBER = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)(?![\w])")
_PENDING = re.compile(r"\{pending\}")

SMALL_INT_MAX = 20  # целые 0–20 без дробной части — счётчики («3 гипотезы»), не результаты


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}


def save(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def add_manual(path: Path, key: str, value: float, unit: str = "", note: str = "") -> dict[str, Any]:
    data = load(path)
    data[key] = {"value": value, "unit": unit, "source": "manual", "note": note,
                 "run": time.strftime("%Y-%m-%dT%H:%M:%S")}
    save(path, data)
    return data


def _matches(num: str, value: Any, rounding: int) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    n = float(num.replace(",", "."))
    decimals = len(re.split(r"[.,]", num)[-1]) if ("," in num or "." in num) else 0
    return round(float(value), decimals) == n or round(float(value), rounding) == round(n, rounding)


def check_numbers(text: str, registry: dict[str, Any], rounding: int = 2) -> list[dict[str, Any]]:
    """Числа в тексте вне кода, формул, ссылок и заголовков, которых нет в реестре и не помечены {pending}.

    ponytail: грубые исключения (счётчики ≤ 20, годы в скобках ссылок); при ложных срабатываниях —
    добавить в профиль журнала список игнорируемых шаблонов.
    """
    values = [v.get("value") if isinstance(v, dict) else v for v in registry.values()]
    blockers = []
    clean = _CODE_BLOCK.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)  # код/формулы → пробелы
    for lineno, line in enumerate(clean.splitlines(), 1):
        if line.lstrip().startswith("#") or _PENDING.search(line):
            continue
        stripped = _LIST_MARKER.sub(" ", _CITATION.sub(" ", line))
        for m in _NUMBER.finditer(stripped):
            num = m.group(1)
            if "," not in num and "." not in num and int(num) <= SMALL_INT_MAX:
                continue
            if any(_matches(num, v, rounding) for v in values):
                continue
            original = text.splitlines()[lineno - 1].strip()
            blockers.append({"line": lineno, "quote": original[:120], "number": num,
                             "issue": f"число {num} отсутствует в реестре результатов",
                             "check": f"grep -n '{num}' artifacts/draft.md; см. results.json"})
    return blockers


__all__ = ["add_manual", "check_numbers", "load", "save"]
