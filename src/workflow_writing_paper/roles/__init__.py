"""Роли: сборка контекста кодом, вызов назначенной модели, реализация стадий novelty / draft / review / assembly."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from ..core.project import Project
from ..providers import Completion, Provider, ProviderError, build_pool, call_logged
from ..review import ReviewError, review_artifact

logger = logging.getLogger(__name__)


class StageError(RuntimeError):
    pass


# --- вызов роли -----------------------------------------------------------------------------
def _provider(project: Project, role: str) -> Provider:
    pid = project.role_provider(role)
    pool = build_pool(project.ws.config())
    if not pid or pid not in pool:
        raise StageError(f"на роль {role} не назначен доступный провайдер (providers.yaml → roles)")
    return pool[pid]


def call_role(project: Project, role: str, user: str, task: str | None = None,
              max_tokens: int = 4000) -> tuple[Completion, Provider]:
    ok, why = project.budget_left()
    if not ok:
        raise StageError(why)
    provider = _provider(project, role)
    messages = [{"role": "system", "content": project.ws.text(f"prompts/{project.language}/{role}.md")},
                {"role": "user", "content": user}]
    db = project.ws.db()
    try:
        comp = call_logged(provider, messages, role=role, log_dir=project.path / "calls", db=db,
                           project=project.slug, task=task, max_tokens=max_tokens, temperature=0.3)
    except ProviderError as e:
        raise StageError(f"{role} ({provider.id}): {e}") from e
    finally:
        db.close()
    return comp, provider


def _created_by(role: str, provider: Provider, comp: Completion | None = None) -> dict[str, Any]:
    return {"role": role, "provider": provider.id, "family": provider.family, "call": comp.log_path if comp else None}


def _blockers_text(blockers: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{b.get('judge', 'судья')}] строка {b.get('line', '?')}: {b.get('issue', '')}"
                     + (f" — «{b['quote']}»" if b.get("quote") else "") + (f" → {b['fix']}" if b.get("fix") else "")
                     for b in blockers)


def _low_score_notes(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Оценка ниже порога без блокеров: замечания судей становятся пунктами к правке."""
    notes = [{"judge": jid, "issue": c} for jid, r in result["reports"].items() for c in (r.get("comments") or [])]
    return notes or [{"judge": "review", "issue": f"итоговая оценка {result['min_overall']} ниже порога "
                                                   f"{result['threshold']}"}]


def _review_loop(project: Project, type_: str, task: str | None,
                 revise: Callable[[list[dict[str, Any]]], None]) -> dict[str, Any]:
    """Проверка; при блокерах — до auto_iterations правок ролью-автором, дальше человек."""
    limit = int(project.review_settings()["auto_iterations"])
    try:
        result = review_artifact(project, type_, task)
        for _ in range(limit):
            if result["accepted"] or (result["errors"] and not result["blockers"]):
                break  # принято — или судей не хватило: правка текста это не исправит
            revise(result["blockers"] or _low_score_notes(result))
            result = review_artifact(project, type_, task)
    except ReviewError as e:
        raise StageError(str(e)) from e
    return result


# --- novelty --------------------------------------------------------------------------------
def stage_novelty(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    seed = str(payload.get("input") or "").strip()
    prev = project.artifact("novelty")
    source = seed or str((prev.meta.get("source") if prev else None) or "")  # только текст автора, не модели
    if not source:
        raise StageError("нужен абзац новизны от автора (payload.input)")
    base = f"{project.context_header(with_registry=False)}\n\n## Формулировка автора\n{source}"
    draft, provider = call_role(project, "ideator", base, task)
    critique, _ = call_role(project, "critic", f"{base}\n\n## Черновик новизны\n{draft.text}", task, 2500)
    final, provider = call_role(project, "ideator", f"{base}\n\n## Твой черновик\n{draft.text}\n\n"
                                f"## Критика\n{critique.text}\n\nПерепиши черновик, ответив на критику: "
                                "исправь обоснованное, аргументированно отклони спорное.", task)
    project.write_artifact("novelty", final.text, _created_by("ideator", provider, final), source=source)

    def revise(blockers: list[dict[str, Any]]) -> None:
        cur = project.artifact("novelty")
        text = cur.body if cur else ""
        fixed, p = call_role(project, "ideator", f"{base}\n\n## Текущая версия\n{text}\n\n## Блокеры проверки\n"
                             f"{_blockers_text(blockers)}\n\nИсправь все блокеры, остальное сохрани.", task)
        project.write_artifact("novelty", fixed.text, _created_by("ideator", p, fixed))

    return _review_loop(project, "novelty", task, revise)


# --- draft ----------------------------------------------------------------------------------
_SECTION = re.compile(r"^##\s*(.+?)\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


def merge_section(body: str, section: str, text: str, order: list[str]) -> str:
    """Заменить раздел `## section` или вставить его по порядку журнала."""
    text = text.strip()
    first = text.splitlines()[0] if text else ""
    if not re.match(rf"^##\s*{re.escape(section)}\s*$", first, re.IGNORECASE):
        text = f"## {section}\n\n{text}"
    parts = {m.group(1).strip(): m.group(0).rstrip() for m in _SECTION.finditer(body)}
    parts = {k if k.lower() != section.lower() else section: v for k, v in parts.items()}
    parts[section] = text
    ordered = [parts[s] for s in order if s in parts] + [v for k, v in parts.items() if k not in order]
    return "\n\n".join(ordered) + "\n"


def _draft_context(project: Project) -> str:
    parts = [project.context_header(with_registry=True)]
    for t in ("novelty", "literature", "experiment_plan"):
        art = project.artifact(t)
        if art and art.status == "approved":
            parts.append(f"## Одобренный артефакт «{t}»\n{art.body}")
    style = project.style()
    if style["samples"]:
        parts.append("## Образцы стиля автора\n" + "\n---\n".join(style["samples"]))
    if style["banned"]:
        parts.append("## Запрещённые фразы\n" + "\n".join(f"- {b}" for b in style["banned"]))
    return "\n\n".join(parts)


def _edit(project: Project, ctx: str, text: str, task: str | None) -> str:
    """Проход редактора по разделу, если роль editor назначена; иначе текст без изменений."""
    if not project.role_provider("editor"):
        return text
    edited, _ = call_role(project, "editor", f"{ctx}\n\n## Раздел для правки\n{text}", task, 6000)
    return edited.text if edited.text.lstrip().startswith("## ") else text  # формат сломан — не берём


def stage_draft(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    section = str(payload.get("section") or "").strip()
    sections = project.sections()
    if section not in sections:
        raise StageError(f"раздел «{section}» не из профиля журнала: {', '.join(sections)}")
    ctx = _draft_context(project)
    prev = project.artifact("draft")
    body_prev = prev.body if prev else ""
    ask = f"{ctx}\n\n## Уже написанные разделы\n{body_prev or '(пусто)'}\n\nНапиши раздел «{section}»."
    comp, provider = call_role(project, "section_writer", ask, task, 6000)
    text = _edit(project, ctx, comp.text, task)
    project.write_artifact("draft", merge_section(body_prev, section, text, sections),
                           _created_by("section_writer", provider, comp))

    def revise(blockers: list[dict[str, Any]]) -> None:
        cur = project.artifact("draft")
        text = cur.body if cur else ""
        fixed, p = call_role(project, "section_writer", f"{ctx}\n\n## Текущий черновик\n{text}\n\n"
                             f"## Блокеры проверки\n{_blockers_text(blockers)}\n\nИсправь блокеры в разделе «{section}»"
                             " и верни только этот раздел целиком.", task, 6000)
        project.write_artifact("draft", merge_section(text, section, fixed.text, sections),
                               _created_by("section_writer", p, fixed))

    return _review_loop(project, "draft", task, revise)


# --- review, assembly ----------------------------------------------------------------------
def stage_review(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    """Проверка целиком: по умолчанию draft; payload.artifact — перепроверить любой артефакт без перегенерации."""
    type_ = str(payload.get("artifact") or "draft")
    if project.artifact(type_) is None:
        raise StageError(f"артефакта {type_} нет")
    try:
        return review_artifact(project, type_, task)
    except ReviewError as e:
        raise StageError(str(e)) from e


def stage_assembly(project: Project, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    draft = project.artifact("draft")
    if draft is None or draft.status != "approved":
        raise StageError("черновик не одобрен")
    title = project.manifest.get("title") or project.slug
    paper = f"# {title}\n\n{draft.body}"
    art = project.write_artifact("paper", paper, {"role": "assembly", "provider": "code", "family": "code"},
                                 status="approved")
    (project.path / "paper.md").write_text(paper, encoding="utf-8")
    outputs = ["paper.md"]
    if shutil.which("pandoc"):
        out = project.path / "paper.docx"
        proc = subprocess.run(["pandoc", str(project.path / "paper.md"), "-o", str(out)], check=False,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            outputs.append(out.name)
        else:
            logger.warning("pandoc: %s", proc.stderr.strip()[-300:])
    return {"accepted": True, "outputs": outputs, "version": art.version}


STAGES = {"novelty": stage_novelty, "draft": stage_draft, "review": stage_review, "assembly": stage_assembly}
NOT_YET = {"literature": "v0.3", "experiment_plan": "v0.3", "experiments": "v0.4"}


def run_stage(project: Project, stage: str, payload: dict[str, Any], task: str | None = None) -> dict[str, Any]:
    if stage in NOT_YET:
        raise StageError(f"стадия {stage} появится в {NOT_YET[stage]}; пока её можно пропустить с указанием причины")
    if stage not in STAGES:
        raise StageError(f"неизвестная стадия {stage}")
    return STAGES[stage](project, payload, task)


__all__ = ["NOT_YET", "STAGES", "StageError", "call_role", "merge_section", "run_stage"]
