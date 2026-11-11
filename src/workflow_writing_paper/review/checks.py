"""Детерминированные проверки артефакта — судья `checker`, каждое срабатывание — блокер."""

from __future__ import annotations

import re
from typing import Any

from ..core.artifact import Artifact
from ..core.project import Project
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
# «табл. 2», «таблице 2», «рис. 3», «рисунка 3», «Table 1», «Fig. 4»; вид — первая буква: т/t или р/f
# ponytail: «таблицы 1 и 2» ловит только первый номер; диапазоны «рис. 2–4» не разворачиваются.
_XREF = re.compile(r"(?i)\b(табл\.|таблиц\w*|рис\.|рисун\w*|tab\.|tables?|figs?\.|figures?)\s*(\d+)")
_CAPTION = re.compile(r"(?i)^[\s*_!\[]*(?:таблица|рисунок|рис\.|table|figure|fig\.)\s*(\d+)\s*[.:—–-]")
_ABSTRACT = re.compile(r"(?i)аннотация|реферат|abstract")


def _kind(word: str) -> str:
    return "table" if word[0].lower() in "тt" else "figure"


def cross_refs(body: str) -> list[dict[str, Any]]:
    """Ссылка «табл. N» без подписи «Таблица N.» и подпись, на которую нет ссылки в тексте."""
    captions: dict[tuple[str, str], int] = {}
    refs: dict[tuple[str, str], int] = {}
    for i, line in enumerate(strip_code(body).splitlines(), 1):
        cap = _CAPTION.match(line)
        if cap:
            word = line.lstrip(" *_![").split()[0]
            captions.setdefault((_kind(word), cap.group(1)), i)
            line = line[cap.end():]  # остаток подписи может ссылаться на другие таблицы
        for m in _XREF.finditer(line):
            refs.setdefault((_kind(m.group(1)), m.group(2)), i)
    names = {"table": ("таблица", "табл."), "figure": ("рисунок", "рис.")}
    out = [{"line": ln, "quote": "", "check": f"grep -n -i '{names[k][0]} {n}'",
            "issue": f"ссылка на {names[k][1]} {n}, но подписи «{names[k][0].capitalize()} {n}» нет"}
           for (k, n), ln in refs.items() if (k, n) not in captions]
    out += [{"line": ln, "quote": "", "check": f"grep -n -i '{names[k][1]} {n}'",
             "issue": f"на {names[k][0]} {n} нет ссылки в тексте"}
            for (k, n), ln in captions.items() if (k, n) not in refs]
    return out


def _section_words(body: str, heading: re.Pattern[str]) -> int | None:
    """Число слов раздела, заголовок которого совпал с `heading`; None — раздела нет."""
    words, found = 0, False
    for line in body.splitlines():
        if line.startswith("#"):
            if found:
                break
            found = bool(heading.search(line))
        elif found:
            words += len(line.split())
    return words if found else None


def journal_limits(project: Project, body: str) -> list[dict[str, Any]]:
    """Лимиты профиля журнала: объём аннотации, объём текста в страницах, минимум источников."""
    limits = project.journal().get("limits") or {}
    out: list[dict[str, Any]] = []
    abstract_max = int(limits.get("abstract_words") or 0)
    n = _section_words(body, _ABSTRACT) if abstract_max else None
    if n is not None and n > abstract_max:
        out.append({"line": 1, "quote": "", "check": "wc -w по разделу аннотации",
                    "issue": f"аннотация {n} слов при лимите журнала {abstract_max}"})
    if limits.get("body_pages"):
        # ponytail: страница ≈ words_per_page слов (по умолчанию 500, кегль 12–14); точный объём — после pandoc
        per_page = int(limits.get("words_per_page", 500))
        pages = len(strip_code(body).split()) / per_page
        if pages > float(limits["body_pages"]):
            out.append({"line": 1, "quote": "", "check": f"wc -w artifacts/draft.md (÷ {per_page})",
                        "issue": f"объём ≈ {pages:.1f} с. при лимите журнала {limits['body_pages']}"})
    refs_min = limits.get("references_min")
    n_refs = len(citation_keys(body))
    if refs_min and n_refs < int(refs_min):
        out.append({"line": 1, "quote": "", "check": "grep -o '@[A-Za-z0-9_:.+/-]*' | sort -u | wc -l",
                    "issue": f"источников {n_refs} при минимуме журнала {refs_min}"})
    return out


def citation_keys(text: str) -> dict[str, int]:
    """Ключи цитирования вне кода и формул → номер первой строки."""
    found: dict[str, int] = {}
    for i, line in enumerate(strip_code(text).splitlines(), 1):
        for m in _CITE_KEY.finditer(line):
            found.setdefault(m.group(1), i)
    return found


def deterministic_checks(project: Project, art: Artifact, rubric: str) -> list[dict[str, Any]]:
    """Язык, обязательные разделы, запрещённые фразы, числа ↔ реестр, ссылки ↔ библиотека; у полного
    черновика — лимиты журнала и перекрёстные ссылки на таблицы и рисунки."""
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
        lib = project.library()
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

    # черновик проверяется после каждого раздела: лимиты и перекрёстные ссылки — только у полного текста
    if art.type == "paper" or (art.type == "draft" and project.draft_complete()):
        blockers += journal_limits(project, body) + cross_refs(body)

    if art.type == "experiment_plan" and not _REGISTRY_KEY.search(body):  # ключи в коде, strip_code не нужен
        blockers.append({"line": 1, "quote": "", "check": "grep -n '`[a-z_]*`' artifacts/experiment_plan.md",
                         "issue": "не объявлен ни один ключ реестра (snake_case в обратных кавычках)"})

    return blockers
