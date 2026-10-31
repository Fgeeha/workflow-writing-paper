"""Детерминированные проверки артефакта — судья `checker`, каждое срабатывание — блокер."""

from __future__ import annotations

import re
from typing import Any

from ..core.artifact import Artifact
from ..core.project import Project
from ..registry import check_numbers
from .rubric import LANGUAGE_MIN_SHARE, language_share

_REQUIRED = re.compile(r"Обязательные разделы:\s*(.+)|Required sections:\s*(.+)")

NUMBER_CHECKED_TYPES = ("draft", "paper")


def deterministic_checks(project: Project, art: Artifact, rubric: str) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    body = art.body
    lines = body.splitlines()

    share = language_share(body, project.language)
    if share < LANGUAGE_MIN_SHARE:
        blockers.append({"line": 1, "quote": "", "issue": f"текст не на языке статьи ({project.language}: {share:.0%})",
                         "check": "language_share"})

    m = _REQUIRED.search(rubric)
    if m:
        for section in [s.strip() for s in (m.group(1) or m.group(2)).split(";") if s.strip()]:
            if not re.search(rf"^#+\s*.*{re.escape(section)}", body, re.MULTILINE | re.IGNORECASE):
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

    return blockers
