"""Детерминированные проверки артефакта — судья `checker`, каждое срабатывание — блокер."""

from __future__ import annotations

import re
from typing import Any

from ..core.artifact import Artifact
from ..core.project import Project
from ..library import Library
from ..registry import check_numbers, strip_code
from .rubric import LANGUAGE_MIN_SHARE, language_share, stems

_REQUIRED = re.compile(r"Обязательные разделы:\s*(.+)|Required sections:\s*(.+)")

NUMBER_CHECKED_TYPES = ("draft", "paper")
CITED_TYPES = ("literature", "draft", "paper")  # ссылки только в форме [@ключ], ключ — из refs.bib
# [@smith2020deep], [@a; @b, p. 5], @smith2020deep — ключ pandoc: буквы, цифры, _ : . + / -
_CITE_KEY = re.compile(r"(?<![\w.@/])@([A-Za-z0-9_][\w:.+/-]*[\w])")  # не e-mail и не https://x.com/@org
# ponytail: ключ реестра — snake_case хотя бы с одним подчёркиванием в обратных кавычках; `fp32` или `pandoc`
# не считаются. Полная проверка «каждое утверждение покрыто» остаётся судьям по рубрике C1.
_REGISTRY_KEY = re.compile(r"`[a-z][a-z0-9]*(?:_[a-z0-9]+)+`")


def citation_keys(text: str) -> dict[str, int]:
    """Ключи цитирования вне кода и формул → номер первой строки."""
    found: dict[str, int] = {}
    for i, line in enumerate(strip_code(text).splitlines(), 1):
        for m in _CITE_KEY.finditer(line):
            found.setdefault(m.group(1), i)
    return found


def deterministic_checks(project: Project, art: Artifact, rubric: str) -> list[dict[str, Any]]:
    """Язык текста (без кода и формул), обязательные разделы рубрики, запрещённые фразы, числа ↔ реестр."""
    blockers: list[dict[str, Any]] = []
    body = art.body
    lines = body.splitlines()

    share = language_share(_CITE_KEY.sub("", strip_code(body)), project.language)  # ключи [@key] — не текст
    if share < LANGUAGE_MIN_SHARE:
        blockers.append({"line": 1, "quote": "", "issue": f"текст не на языке статьи ({project.language}: {share:.0%})",
                         "check": "language_share"})

    m = _REQUIRED.search(rubric)
    if m:
        headings = [h.lower() for h in re.findall(r"^#+\s*(.+)$", body, re.MULTILINE)]
        for section in [s.strip() for s in (m.group(1) or m.group(2)).split(";") if s.strip()]:
            if not any(stems(section) <= stems(h) for h in headings):
                blockers.append({"line": 1, "quote": "", "issue": f"нет обязательного раздела «{section}»",
                                 "check": f"grep -n '^#.*{section}'"})

    for phrase in project.style()["banned"]:
        for i, line in enumerate(lines, 1):
            if phrase.lower() in line.lower():
                blockers.append({"line": i, "quote": line.strip()[:120], "issue": f"запрещённая фраза «{phrase}»",
                                 "check": f"grep -n -i '{phrase}'"})

    if art.type in NUMBER_CHECKED_TYPES:
        rounding = int((project.journal().get("numbers") or {}).get("rounding", 2))
        blockers += check_numbers(body, project.registry_data(), rounding)

    if art.type in CITED_TYPES:
        cited = citation_keys(body)
        lib = Library(project.ws.root / "library")
        citable, all_keys = lib.citable_keys(), lib.keys()
        for key, lineno in cited.items():
            if key in citable:
                continue
            why = ("запись-заглушка PDF без метаданных; подтвердите запись через OpenAlex или .bib" if key in all_keys
                   else "выдуманная или не импортированная")
            blockers.append({"line": lineno, "quote": lines[lineno - 1].strip()[:120],
                             "check": f"grep -n '{key}' library/refs.bib",
                             "issue": f"ссылка [@{key}] не из refs.bib — {why}"})
        if art.type == "literature" and not cited:
            blockers.append({"line": 1, "quote": "", "issue": "в обзоре нет ни одной ссылки вида [@ключ]",
                             "check": "grep -c '\\[@' artifacts/literature.md"})

    if art.type == "experiment_plan" and not _REGISTRY_KEY.search(body):  # ключи в коде, strip_code не нужен
        blockers.append({"line": 1, "quote": "", "check": "grep -n '`[a-z_]*`' artifacts/experiment_plan.md",
                         "issue": "не объявлен ни один ключ реестра (snake_case в обратных кавычках)"})

    return blockers
