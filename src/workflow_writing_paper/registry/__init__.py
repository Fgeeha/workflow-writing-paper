"""Реестр результатов `results.json` — единственный источник чисел в тексте статьи."""

from __future__ import annotations

import json
import math
import re
import time
from decimal import ROUND_HALF_UP, Context, Decimal
from pathlib import Path
from typing import Any

_WIDE = Context(prec=400)  # quantize 1e27 до сотых требует больше 28 знаков по умолчанию
_CODE_BLOCK = re.compile(r"```.*?```|`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$", re.DOTALL)
# [key] / [Иванов, 2021] и (Иванов, 2021) / (Smith et al., 2021a) — но не «(задержка 95,5 мс на 1000 снимках)»
_CITATION = re.compile(r"\[[^\]\n]*\]|\((?:[^()\n\d]*?[,\s])?(?:19|20)\d{2}[a-zа-я]?\)")
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
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return False
    n = Decimal(num.replace(",", "."))
    decimals = len(re.split(r"[.,]", num)[-1]) if ("," in num or "." in num) else 0
    return _round(value, decimals) == n or _round(value, rounding) == _round(n, rounding)


def _round(value: Any, decimals: int) -> Decimal:
    """Округление «как в статье» — половина вверх по десятичной записи: 3.505 → 3.51 (round() даёт 3.5)."""
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP, context=_WIDE)


def check_numbers(text: str, registry: dict[str, Any], rounding: int = 2) -> list[dict[str, Any]]:
    """Числа в тексте вне кода, формул, ссылок и заголовков, которых нет в реестре и не помечены {pending}.

    ponytail: грубые исключения (счётчики ≤ 20, годы в скобках ссылок); при ложных срабатываниях —
    добавить в профиль журнала список игнорируемых шаблонов.
    """
    values = [v.get("value") if isinstance(v, dict) else v for v in registry.values()]
    blockers = []
    originals = text.splitlines()
    clean = strip_code(text)
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
            original = originals[lineno - 1].strip()
            blockers.append({"line": lineno, "quote": original[:120], "number": num,
                             "issue": f"число {num} отсутствует в реестре результатов",
                             "check": f"grep -n '{num}' artifacts/draft.md; см. results.json"})
    return blockers


def strip_code(text: str) -> str:
    """Код и формулы → пробелы (строки сохраняются)."""
    return _CODE_BLOCK.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


__all__ = ["add_manual", "check_numbers", "load", "save", "strip_code"]
